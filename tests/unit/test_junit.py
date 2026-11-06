"""Per-test results (ADR-0044): safe JUnit reading, tool outputs, the runner's reports."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_coding.quality.junit import (
    DETAILS_LIMIT,
    MAX_REPORT,
    JUnitError,
    from_cargo_test,
    from_go_test_json,
    from_tool_output,
    parse_junit,
    project_path,
    summarize,
    to_junit_xml,
)
from ai_coding.tools.testrun import JUNIT_PATH, plan_tests, read_report

# Real reports (trimmed): pytest -o junit_family=xunit1 (lines are 0-based), Vitest,
# node --test (node:22) — as they come out of the sandbox, where the project is /work.
PYTEST = b"""<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">
<testsuite name="pytest" errors="0" failures="1" skipped="1" tests="3" time="0.051">
<testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py" line="3" time="0.001" />
<testcase classname="tests.test_a" name="test_bad" file="tests/test_a.py" line="7" time="0.25">
<failure message="assert 1 == 2">def test_bad():
        x = 1
&gt;       assert x == 2
E       assert 1 == 2

tests/test_a.py:10: AssertionError</failure></testcase>
<testcase classname="tests.test_a" name="test_skip" file="tests/test_a.py" line="12" time="0.000">
<skipped type="pytest.skip" message="later">/work/tests/test_a.py:13: later</skipped></testcase>
</testsuite></testsuites>"""

VITEST = b"""<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="2" failures="1" errors="0" time="2.199">
  <testsuite name="src/a.test.ts" tests="2" failures="1" errors="0" skipped="0" time="0.26">
    <testcase classname="src/a.test.ts" name="sum &gt; adds" time="0.0012"></testcase>
    <testcase classname="src/a.test.ts" name="sum &gt; fails" time="0.004">
      <failure message="expected 3 to be 4" type="AssertionError">AssertionError: expected 3 to be 4
 \xe2\x9d\xaf node_modules/vitest/dist/chunk.js:10:3
 \xe2\x9d\xaf src/a.test.ts:9:17</failure>
    </testcase>
  </testsuite>
</testsuites>"""

NODE = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites>
\t<testsuite name="math" time="0.002624" disabled="0" errors="0" tests="3" failures="1" skipped="1">
\t\t<testcase name="one" time="0.000688" classname="test"/>
\t\t<testcase name="two" time="0.000688" classname="test" failure="1 == 2">
\t\t\t<failure type="testCodeFailure" message="1 == 2">
Error [ERR_TEST_FAILURE]: 1 == 2
  cause: AssertionError [ERR_ASSERTION]: 1 == 2
      at TestContext.&lt;anonymous> (file:///work/test/a.test.js:5:28)
      at Test.runInAsyncScope (node:async_hooks:214:14)
\t\t\t</failure>
\t\t</testcase>
\t\t<testcase name="later" time="0.000167" classname="test">
\t\t\t<skipped type="skipped" message="not yet"/>
\t\t</testcase>
\t</testsuite>
\t<!-- tests 3 -->
</testsuites>"""


def cases(suites):  # type: ignore[no-untyped-def]
    return {c.name: c for s in suites for c in s.cases}


# ------------------------------------------------------------------ reading --
def test_pytest_report_points_at_the_failing_line() -> None:
    suites = parse_junit(PYTEST, tool="pytest")
    assert [s.name for s in suites] == ["pytest"]
    by = cases(suites)
    assert by["test_ok"].status == "passed" and by["test_ok"].line == 4  # 0-based → 1-based
    bad = by["test_bad"]
    assert (bad.status, bad.file, bad.line) == ("failed", "tests/test_a.py", 10)
    assert bad.message == "assert 1 == 2" and "AssertionError" in bad.details
    assert bad.key == "tests.test_a::test_bad"
    assert by["test_skip"].status == "skipped" and by["test_skip"].message == "later"
    totals = summarize(suites)
    assert totals == {"tests": 3, "passed": 1, "failed": 1, "errors": 0, "skipped": 1,
                      "time": 0.051}  # fmt: skip


def test_vitest_report_uses_the_classname_as_the_file() -> None:
    by = cases(parse_junit(VITEST, tool="vitest"))
    assert by["sum > adds"].file == "src/a.test.ts" and by["sum > adds"].line is None
    fails = by["sum > fails"]
    # the first project location in the stack, not the library frame before it
    assert (fails.file, fails.line) == ("src/a.test.ts", 9)
    assert fails.message == "expected 3 to be 4"


def test_node_report_paths_are_relative_to_the_project() -> None:
    suites = parse_junit(NODE, tool="node")
    by = cases(suites)
    two = by["two"]
    assert (two.status, two.file, two.line) == ("failed", "test/a.test.js", 5)
    assert two.classname == "math" and two.key == "math::two"  # not node's constant "test"
    assert by["later"].status == "skipped" and by["one"].file is None
    data = suites[0].to_dict()
    assert [c["status"] for c in data["cases"]] == ["failed", "passed", "skipped"]  # failed first
    assert (data["tests"], data["failures"], data["skipped"]) == (3, 1, 1)


def test_error_status_and_loose_cases() -> None:
    xml = (b"<testsuites><testcase name='a' classname='k' time='x'>"
           b"<error message='boom' type='RuntimeError'>trace</error></testcase>"
           b"<testcase classname='k'/></testsuites>")  # fmt: skip
    suites = parse_junit(xml)
    assert [s.name for s in suites] == ["tests"]
    a, unnamed = suites[0].cases
    assert (a.status, a.message, a.time) == ("error", "boom", 0.0)
    assert unnamed.name == "(без имени)"


def test_nested_suites_get_joined_names() -> None:
    xml = (b'<testsuites><testsuite name="outer"><testsuite name="inner">'
           b'<testcase name="t"/></testsuite></testsuite></testsuites>')  # fmt: skip
    assert [s.name for s in parse_junit(xml)] == ["outer › inner"]


def test_details_are_clipped() -> None:
    xml = (b"<testsuite><testcase name='a'><failure message='m'>"
           + b"x" * (DETAILS_LIMIT * 3) + b"</failure></testcase></testsuite>")  # fmt: skip
    data = parse_junit(xml)[0].to_dict()["cases"][0]
    assert len(data["details"]) < DETAILS_LIMIT + 40 and data["details"].endswith("more]")


# -------------------------------------------------------------------- safety --
@pytest.mark.parametrize(
    "xml",
    [
        # billion laughs
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;">]><testsuite><testcase name="&lol2;"/></testsuite>',
        # external entity (file read)
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<testsuite><testcase name="&xxe;"/></testsuite>',
        # external DTD
        b'<!DOCTYPE testsuite SYSTEM "http://example.com/x.dtd"><testsuite/>',
        # case games do not get past the check
        b"<!doctype testsuite><testsuite/>",
    ],
)
def test_doctype_and_entities_are_refused(xml: bytes) -> None:
    with pytest.raises(JUnitError, match="DOCTYPE"):
        parse_junit(xml)


def test_undeclared_entity_is_an_error_not_an_expansion() -> None:
    with pytest.raises(JUnitError, match="well-formed"):
        parse_junit(b'<testsuite><testcase name="&xxe;"/></testsuite>')


@pytest.mark.parametrize("data", [b"", b"not xml", b"<testsuite><testcase>", b"\x00\xff"])
def test_malformed_reports_are_errors(data: bytes) -> None:
    with pytest.raises(JUnitError):
        parse_junit(data)


def test_size_and_depth_limits() -> None:
    with pytest.raises(JUnitError, match="larger"):
        parse_junit(b" " * (MAX_REPORT + 1))
    with pytest.raises(JUnitError, match="deeply"):
        parse_junit(b"<a>" * 70 + b"</a>" * 70)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_a.py", "tests/test_a.py"),
        ("./src/a.ts", "src/a.ts"),
        ("/work/src/a.ts", "src/a.ts"),
        ("file:///work/test/a.test.js", "test/a.test.js"),
        ("///work/x.py", "x.py"),
        ("/usr/lib/python3.12/unittest/case.py", None),
        ("/deps/pkg/mod.py", None),
        ("node_modules/vitest/dist/x.js", None),
        ("../../etc/passwd", None),
        ("node:internal/test_runner/test", None),
        ("", None),
        (None, None),
    ],
)
def test_project_path(path: str | None, expected: str | None) -> None:
    assert project_path(path) == expected


def test_foreign_file_attributes_are_dropped() -> None:
    xml = b'<testsuite><testcase name="a" file="/etc/passwd" line="3"/></testsuite>'
    case = parse_junit(xml)[0].cases[0]
    assert case.file is None and case.line is None


# ---------------------------------------------------------------- round trip --
def test_round_trip_keeps_the_results() -> None:
    suites = parse_junit(PYTEST, tool="pytest") + parse_junit(NODE, tool="node")
    again = parse_junit(to_junit_xml(suites), tool="again")
    assert summarize(again)["tests"] == 6
    by = cases(again)
    assert (by["test_bad"].file, by["test_bad"].line, by["test_bad"].status) == (
        "tests/test_a.py", 10, "failed")  # fmt: skip
    assert by["two"].message == "1 == 2" and by["later"].status == "skipped"


def test_written_report_escapes_hostile_text() -> None:
    hostile = b"&lt;!DOCTYPE x&gt; ]]&gt; &amp;"
    suites = parse_junit(b'<testsuite><testcase name="a"><failure message="m">'
                         + hostile + b"</failure></testcase></testsuite>")  # fmt: skip
    again = parse_junit(to_junit_xml(suites))
    assert "<!DOCTYPE x> ]]> &" in again[0].cases[0].details


# -------------------------------------------------------------- tool outputs --
def _go(events: list[dict[str, object]]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\nnot json\n"


def test_go_test_json(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.22\n")
    pkg = "example.com/app/calc"
    out = _go([
        {"Action": "run", "Package": pkg, "Test": "TestAdd"},
        {"Action": "pass", "Package": pkg, "Test": "TestAdd", "Elapsed": 0.01},
        {"Action": "output", "Package": pkg, "Test": "TestDiv/zero",
         "Output": "    calc_test.go:21: want error, got nil\n"},
        {"Action": "fail", "Package": pkg, "Test": "TestDiv/zero", "Elapsed": 0.02},
        {"Action": "fail", "Package": pkg, "Test": "TestDiv", "Elapsed": 0.02},
        {"Action": "skip", "Package": pkg, "Test": "TestSlow", "Elapsed": 0},
        {"Action": "fail", "Package": pkg, "Elapsed": 0.5},
        {"Action": "pass", "Package": "example.com/app", "Test": "TestMain", "Elapsed": 0.1},
    ])  # fmt: skip
    suites = from_go_test_json(out, tmp_path)
    assert [s.name for s in suites] == [pkg, "example.com/app"]
    by = cases(suites)
    zero = by["TestDiv/zero"]
    assert (zero.status, zero.file, zero.line) == ("failed", "calc/calc_test.go", 21)
    assert zero.message == "calc_test.go:21: want error, got nil"
    assert by["TestSlow"].status == "skipped" and by["TestAdd"].time == 0.01
    assert suites[0].time == 0.5
    assert from_tool_output("go-test", out, tmp_path) == suites
    assert from_go_test_json("") == []


CARGO = """\
   Compiling app v0.1.0 (/work)
    Finished `test` profile [unoptimized + debuginfo] target(s) in 1.2s
     Running unittests src/lib.rs (target/debug/deps/app-1234)

running 3 tests
test tests::adds ... ok
test tests::divides ... FAILED
test tests::slow ... ignored

failures:

---- tests::divides stdout ----

thread 'tests::divides' panicked at src/lib.rs:17:9:
assertion `left == right` failed
  left: 1
 right: 2
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace


failures:
    tests::divides

test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out

     Running tests/api.rs (target/debug/deps/api-5678)

running 1 test
test health ... ok
"""


def test_cargo_test_text() -> None:
    suites = from_cargo_test(CARGO)
    assert [s.name for s in suites] == ["src/lib.rs", "tests/api.rs"]
    by = cases(suites)
    divides = by["divides"]
    assert (divides.status, divides.file, divides.line) == ("failed", "src/lib.rs", 17)
    assert divides.classname == "tests" and divides.key == "tests::divides"
    assert divides.message == "assertion `left == right` failed"
    assert "left: 1" in divides.details
    assert by["slow"].status == "skipped" and by["health"].classname == ""
    assert from_tool_output("cargo-test", CARGO) == suites
    assert from_tool_output("semgrep", CARGO) == []


# ------------------------------------------------------- the runner's report --
def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_plans_ask_for_a_junit_report(tmp_path: Path) -> None:
    py = plan_tests(write(tmp_path / "p", {"pyproject.toml": "[project]\nname='p'\n",
                                           "tests/test_a.py": "def test_a(): pass\n"}))  # fmt: skip
    assert py is not None and f"--junitxml={JUNIT_PATH}" in py.command
    assert py.command[-2:] == ("-o", "junit_family=xunit1")
    vitest = plan_tests(write(tmp_path / "v", {"package.json": json.dumps(
        {"scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "2"}})}))  # fmt: skip
    assert vitest is not None and vitest.env.get("CI") == "true"
    assert vitest.command[:4] == ("npm", "test", "--silent", "--")
    assert f"--outputFile.junit={JUNIT_PATH}" in vitest.command
    node = plan_tests(write(tmp_path / "n", {"package.json": json.dumps(
        {"scripts": {"test": "node --test"}})}))  # fmt: skip
    assert node is not None and f"--test-reporter-destination={JUNIT_PATH}" in node.command
    jest = plan_tests(write(tmp_path / "j", {"package.json": json.dumps(
        {"scripts": {"test": "jest --ci"}})}))  # fmt: skip
    assert jest is not None and jest.command == ("npm", "test", "--silent")  # no JUnit
    files = plan_tests(write(tmp_path / "f", {"package.json": json.dumps(
        {"scripts": {"test": "node --test test/"}})}))  # fmt: skip
    assert files is not None and files.command == ("npm", "test", "--silent")  # files: no flags


def test_read_report_takes_only_a_regular_file(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    assert read_report(report) is None  # none written
    report.write_bytes(b"")
    assert read_report(report) is None  # empty
    report.write_bytes(PYTEST)
    assert read_report(report) == PYTEST
    assert read_report(report, limit=10) is None  # too large
    secret = tmp_path / "secret"
    secret.write_text("host file")
    link = tmp_path / "link.xml"
    link.symlink_to(secret)  # the sandbox plants a link to a host path
    assert read_report(link) is None
    folder = tmp_path / "dir.xml"
    folder.mkdir()
    assert read_report(folder) is None
    fifo = tmp_path / "fifo.xml"
    os.mkfifo(fifo)
    assert read_report(fifo) is None  # never blocks on a pipe
