"""Tool adapters, fingerprints, registry and runner (with a test-double executor)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ai_coding.domain.enums import ExecutionBackend, Severity, Stack
from ai_coding.executors import CommandSpec, ExecutionResult, ExecutorError
from ai_coding.tools import ToolRegistry, ToolRun, ToolRunner, default_tool_registry
from ai_coding.tools.base import ToolCategory, fingerprint, relative_path
from ai_coding.tools.gitleaks import GitleaksAdapter
from ai_coding.tools.ruff import RuffAdapter
from ai_coding.tools.semgrep import REGISTRY_CONFIG, SemgrepAdapter, bundled_rules_dir

GITLEAKS_OUT = json.dumps(
    [
        {
            "RuleID": "aws-access-token",
            "Description": "AWS key",
            "File": "/src/app/settings.py",
            "StartLine": 3,
            "Match": "REDACTED",
            "Secret": "REDACTED",
            "Fingerprint": "app/settings.py:aws:3",
            "Entropy": 3.6,
        },
    ]
)
SEMGREP_OUT = json.dumps(
    {
        "results": [
            {
                "check_id": "rules.python-eval-exec",
                "path": "/src/app/main.py",
                "start": {"line": 6},
                "extra": {
                    "severity": "ERROR",
                    "message": "eval is bad\nmore",
                    "lines": "    return eval(cmd)",
                    "metadata": {"cwe": "CWE-95"},
                },
            },
            {
                "check_id": "rules.python-weak-hash",
                "path": "/src/app/main.py",
                "start": {"line": 9},
                "extra": {"severity": "WARNING", "message": "md5", "lines": "md5()"},
            },
        ],
        "errors": [],
    }
)
RUFF_OUT = json.dumps(
    [
        {
            "code": "F401",
            "filename": "/repo/app/main.py",
            "message": "`os` imported but unused",
            "location": {"row": 1, "column": 8},
            "url": "https://docs.astral.sh/ruff/rules/unused-import",
        },
        {
            "code": "S307",
            "filename": "/repo/app/main.py",
            "message": "eval",
            "location": {"row": 6},
        },
        {
            "code": "E501",
            "filename": "/repo/app/main.py",
            "message": "long",
            "location": {"row": 7},
        },
        {
            "code": None,
            "filename": "/repo/app/bad.py",
            "message": "SyntaxError",
            "location": {"row": 2},
        },
    ]
)


def test_gitleaks_parse_never_keeps_secret() -> None:
    [finding] = GitleaksAdapter().parse(GITLEAKS_OUT, target="/src")
    assert finding.path == "app/settings.py" and finding.line == 3
    assert finding.severity is Severity.HIGH and finding.rule_id == "aws-access-token"
    assert "REDACTED" not in json.dumps(finding.to_dict())
    assert GitleaksAdapter().parse("", target="/src") == []
    with pytest.raises(TypeError):
        GitleaksAdapter().parse('{"not": "a list"}', target="/src")


def test_semgrep_parse_and_severity() -> None:
    findings = SemgrepAdapter().parse(SEMGREP_OUT, target="/src")
    assert [(f.rule_id, f.severity, f.path, f.line) for f in findings] == [
        ("python-eval-exec", Severity.HIGH, "app/main.py", 6),
        ("python-weak-hash", Severity.MEDIUM, "app/main.py", 9),
    ]
    assert findings[0].title == "eval is bad" and findings[0].evidence["cwe"] == "CWE-95"
    with pytest.raises(ValueError, match="semgrep reported errors"):
        SemgrepAdapter().parse(
            json.dumps({"results": [], "errors": [{"level": "error", "message": "bad rule"}]}),
            target="/src",
        )


def test_ruff_parse() -> None:
    findings = RuffAdapter().parse(RUFF_OUT, target="/repo")
    by_rule = {f.rule_id: f for f in findings}
    assert by_rule["F401"].severity is Severity.MEDIUM and by_rule["F401"].path == "app/main.py"
    assert by_rule["S307"].severity is Severity.MEDIUM
    assert by_rule["E501"].severity is Severity.LOW
    assert by_rule["syntax-error"].severity is Severity.HIGH
    assert RuffAdapter().parse("", target="/repo") == []
    with pytest.raises(TypeError):
        RuffAdapter().parse("{}", target="/repo")


def test_fingerprint_survives_line_shifts_but_not_content_changes() -> None:
    a = fingerprint("semgrep", "r", "a.py", "  return eval(x)")
    assert a == fingerprint("semgrep", "r", "a.py", "return   eval(x)")
    assert a != fingerprint("semgrep", "r", "a.py", "return eval(y)")
    assert a != fingerprint("semgrep", "r", "b.py", "return eval(x)")
    assert len(a) == 64


@pytest.mark.parametrize(
    ("path", "target", "expected"),
    [
        ("/src/a/b.py", "/src", "a/b.py"),
        ("./a.py", "/x", "a.py"),
        (None, "/src", None),
        ("C:\\repo\\a.py", "C:\\repo", "a.py"),
        ("/other/a.py", "/src", "/other/a.py"),
    ],
)
def test_relative_path(path: str | None, target: str, expected: str | None) -> None:
    assert relative_path(path, target) == expected


def test_versions_and_args() -> None:
    assert GitleaksAdapter().parse_version("8.30.1") == "8.30.1"
    assert SemgrepAdapter().parse_version("1.183.0\n") == "1.183.0"
    assert RuffAdapter().parse_version("ruff 0.16.9") == "0.16.9"
    assert RuffAdapter().parse_version("nothing") is None
    assert "--fix" not in RuffAdapter().scan_args("/src", extra={})
    assert "--no-fix" in RuffAdapter().scan_args("/src", extra={})
    assert "--redact" in GitleaksAdapter().scan_args("/src", extra={})
    assert "--metrics=off" in SemgrepAdapter().scan_args("/src", extra={"config": "r.yml"})
    with pytest.raises(ValueError, match="rules config"):
        SemgrepAdapter().scan_args("/src", extra={})
    for adapter in (GitleaksAdapter(), SemgrepAdapter(), RuffAdapter()):
        assert "@sha256:" in adapter.spec.image


def test_registry_rules_and_selection() -> None:
    reg = default_tool_registry()
    assert reg.names() == [
        "cargo-test",
        "clippy",
        "eslint",
        "gitleaks",
        "go-test",
        "golangci-lint",
        "k6",
        "mypy",
        "osv-scanner",
        "phpstan",
        "pint",
        "playwright",
        "ruff",
        "semgrep",
        "trivy",
        "tsc",
    ]
    assert [a.spec.name for a in reg.select([Stack.GO])] == [
        "gitleaks",
        "go-test",
        "golangci-lint",
        "k6",
        "osv-scanner",
        "semgrep",
        "trivy",
    ]
    assert [a.spec.name for a in reg.select([Stack.PYTHON])] == [
        "gitleaks",
        "k6",
        "mypy",
        "osv-scanner",
        "ruff",
        "semgrep",
        "trivy",
    ]
    assert [a.spec.name for a in reg.select([Stack.LARAVEL, Stack.REACT])] == [
        "eslint",
        "gitleaks",
        "k6",
        "osv-scanner",
        "phpstan",
        "pint",
        "playwright",
        "semgrep",
        "trivy",
        "tsc",
    ]
    assert [a.spec.name for a in reg.select([Stack.GO], only=["ruff"])] == []
    with pytest.raises(KeyError, match="unknown tool"):
        reg.get("nope")
    with pytest.raises(ValueError, match="already registered"):
        reg.register(RuffAdapter())
    with pytest.raises(TypeError):
        ToolRegistry([object()])  # type: ignore[list-item]
    assert (bundled_rules_dir() / "ai-coding-baseline.yml").is_file()
    assert REGISTRY_CONFIG.match("p/python") and not REGISTRY_CONFIG.match("rules.yml")


# ------------------------------------------------------------------ runner --
@dataclass
class FakeExecutor:
    """Test double for the Executor port: scripted results, records specs."""

    backend: ExecutionBackend = ExecutionBackend.DOCKER
    results: dict[str, ExecutionResult] = field(default_factory=dict)
    specs: list[CommandSpec] = field(default_factory=list)
    is_available: bool = True
    raise_on: str | None = None

    def available(self) -> bool:
        return self.is_available

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        self.specs.append(spec)
        key = "version" if spec.argv[1] in {"version", "--version"} else "scan"
        if self.raise_on == key:
            raise ExecutorError("boom")
        return self.results.get(key) or _res(0, "1.2.3")


def _res(code: int, out: str, **kw: object) -> ExecutionResult:
    return ExecutionResult(
        backend=ExecutionBackend.DOCKER,
        argv=("x",),
        exit_code=code,
        stdout=out,
        stderr=kw.pop("stderr", ""),
        duration_seconds=0.1,
        **kw,
    )  # type: ignore[arg-type]


async def test_runner_sandbox_mounts_and_semgrep_config(tmp_path: Path) -> None:
    ex = FakeExecutor(results={"scan": _res(0, SEMGREP_OUT)})
    run = await ToolRunner(ex, tmp_path).run(SemgrepAdapter())
    assert run.succeeded and run.version == "1.2.3" and len(run.findings) == 2
    scan = ex.specs[-1]
    assert scan.image == SemgrepAdapter.spec.image and scan.workdir == "/src"
    assert {m.target for m in scan.mounts} == {"/src", "/rules"}
    assert all(m.read_only for m in scan.mounts)
    assert "/rules/ai-coding-baseline.yml" in scan.argv
    assert run.to_dict()["findings"][0]["path"] == "app/main.py"
    assert ToolRun.from_dict(run.to_dict(include_raw=True)) == run


async def test_runner_local_paths(tmp_path: Path) -> None:
    ex = FakeExecutor(backend=ExecutionBackend.LOCAL, results={"scan": _res(0, "[]")})
    run = await ToolRunner(ex, tmp_path).run(RuffAdapter())
    malformed = await ToolRunner(ex, tmp_path).run(SemgrepAdapter())  # "[]" is not semgrep JSON
    assert not malformed.succeeded and "not a JSON object" in (malformed.error or "")
    assert run.succeeded and str(tmp_path.resolve()) in ex.specs[-1].argv
    assert ex.specs[-1].image is None
    custom = tmp_path / "my-rules.yml"
    custom.write_text("rules: []\n", encoding="utf-8")
    await ToolRunner(ex, tmp_path, semgrep_config=custom).run(SemgrepAdapter())
    assert str(custom) in ex.specs[-1].argv


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (_res(0, "[]", timed_out=True), "timed out"),
        (_res(0, "[]", stdout_truncated=True), "size limit"),
        (_res(2, "", stderr="line1\nfatal: crashed"), "exit code 2: line1 | fatal: crashed"),
        (_res(0, "not json"), "could not parse"),
    ],
)
async def test_runner_failures_are_reported_not_hidden(
    tmp_path: Path, result: ExecutionResult, error: str
) -> None:
    run = await ToolRunner(FakeExecutor(results={"scan": result}), tmp_path).run(RuffAdapter())
    assert not run.succeeded and run.findings == ()
    assert error in (run.error or "")


async def test_runner_version_cached_and_errors(tmp_path: Path) -> None:
    ex = FakeExecutor(results={"scan": _res(0, "[]")})
    runner = ToolRunner(ex, tmp_path)
    await runner.run(RuffAdapter())
    await runner.run(RuffAdapter())
    assert sum(1 for s in ex.specs if "--version" in s.argv) == 1
    broken = ToolRunner(FakeExecutor(raise_on="version", results={"scan": _res(0, "[]")}), tmp_path)
    assert (await broken.run(RuffAdapter())).version is None
    failing = ToolRunner(FakeExecutor(results={"version": _res(1, "")}), tmp_path)
    assert await failing.version(RuffAdapter()) is None


def test_runner_availability(tmp_path: Path) -> None:
    assert ToolRunner(FakeExecutor(), tmp_path).available(RuffAdapter())[0]
    assert not ToolRunner(FakeExecutor(is_available=False), tmp_path).available(RuffAdapter())[0]
    local = ToolRunner(FakeExecutor(backend=ExecutionBackend.LOCAL), tmp_path)
    ok, reason = local.available(GitleaksAdapter())
    assert ok == (reason != "'gitleaks' not found on PATH")
    assert ToolRunner(FakeExecutor(), tmp_path).sandboxed
    assert ToolCategory.SECRETS.value == "secrets"


async def test_version_probe_is_short_and_offline_safe(tmp_path: Path) -> None:
    ex = FakeExecutor(results={"scan": _res(0, SEMGREP_OUT)})
    await ToolRunner(ex, tmp_path, timeout_seconds=900).run(SemgrepAdapter())
    version_spec = next(s for s in ex.specs if "--version" in s.argv)
    assert version_spec.timeout_seconds == 60.0
    assert "--disable-version-check" in version_spec.argv  # no update check that can hang offline
    assert dict(ex.specs[-1].env)["SEMGREP_SEND_METRICS"] == "off"
    assert ex.specs[-1].timeout_seconds == 900
