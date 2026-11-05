"""S7 tool adapters: parsers on real tool output, applicability plans, the project-tool
runner (sandbox-only, install gating, read-only vs scratch copy) and offline data."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ai_coding.domain.enums import ExecutionBackend, Severity
from ai_coding.executors.base import CommandSpec, ExecutionResult, ExecutorError
from ai_coding.tools.base import ToolCategory, ToolRun, ToolSpec
from ai_coding.tools.cache import ENV, cache_root, tool_cache
from ai_coding.tools.golang import GolangciLintAdapter, GoTestAdapter
from ai_coding.tools.mypy import MypyAdapter
from ai_coding.tools.node import ESLintAdapter, TypeScriptAdapter
from ai_coding.tools.osv import OSVScannerAdapter
from ai_coding.tools.php import COMPOSER_HOSTS, PHPStanAdapter, PintAdapter
from ai_coding.tools.prepare import (
    cache_name,
    is_prepared,
    needs_prepare,
    prepare,
    readable_argv,
)
from ai_coding.tools.project import DEPS, WORK, Invocation, ProjectToolRunner
from ai_coding.tools.rust import CLIPPY_CACHE, CargoTestAdapter, ClippyAdapter
from ai_coding.tools.trivy import TrivyAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tools"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def project(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "platform-cache"
    monkeypatch.setenv(ENV, str(cache))
    return cache


# ------------------------------------------------------------------ parsers --
def test_trivy_parses_vulnerabilities_and_misconfigurations() -> None:
    findings = TrivyAdapter().parse(fixture("trivy.json"), target="/src")
    rules = {f.rule_id for f in findings}
    assert {"CVE-2020-8203", "CVE-2021-23337"} <= rules
    lodash = next(f for f in findings if f.rule_id == "CVE-2020-8203")
    assert lodash.path == "package-lock.json" and lodash.severity is Severity.HIGH
    assert "lodash 4.17.15" in lodash.title
    assert any(f.path == "Dockerfile" for f in findings)  # misconfiguration
    assert len({f.fingerprint for f in findings}) == len(findings)


def test_go_test_failures_build_errors_and_coverage() -> None:
    adapter = GoTestAdapter()
    failed = adapter.parse(fixture("go-test.jsonl"), "", 1)
    assert [(f.rule_id, f.path) for f in failed] == [("go-test-failure", "example.com/calc")]
    assert "TestSub" in failed[0].title and failed[0].severity is Severity.HIGH
    assert adapter.coverage(fixture("go-test.jsonl"), "") == 100.0
    broken = adapter.parse(fixture("go-test-build-fail.jsonl"), "", 1)
    assert [f.rule_id for f in broken] == ["go-build-failure"]
    assert adapter.coverage(fixture("go-test-build-fail.jsonl"), "") is None


def test_golangci_lint() -> None:
    (f,) = GolangciLintAdapter().parse(fixture("golangci-lint.json"), "", 1)
    assert (f.rule_id, f.path, f.line) == ("govet", "calc.go", 8)


def test_clippy_and_cargo_test() -> None:
    clippy = ClippyAdapter().parse(fixture("clippy.jsonl"), "", 0)
    assert {(f.rule_id, f.path, f.line) for f in clippy} == {
        ("unused_variables", "src/lib.rs", 2),
        ("clippy::eq_op", "src/lib.rs", 7),
    }
    (failure,) = CargoTestAdapter().parse(fixture("cargo-test.txt"), "", 101)
    assert (failure.rule_id, failure.path, failure.line) == ("cargo-test-failure", "src/lib.rs", 16)
    assert "tests::subs" in failure.title


def test_node_parsers() -> None:
    eslint = ESLintAdapter().parse(fixture("eslint.json"), "", 1)
    assert [(f.rule_id, f.path, f.line) for f in eslint] == [
        ("no-unused-vars", "src/a.js", 1),
        ("no-undef", "src/a.js", 2),
    ]
    (tsc,) = TypeScriptAdapter().parse(fixture("tsc.txt"), "", 2)
    assert (tsc.rule_id, tsc.path, tsc.line) == ("TS2322", "src/b.ts", 1)


def test_php_parsers() -> None:
    phpstan = PHPStanAdapter().parse(fixture("phpstan.json"), "", 1)
    assert {f.rule_id for f in phpstan} == {"missingType.iterableValue", "method.notFound"}
    assert all(f.path == "app/Cart.php" and f.line for f in phpstan)
    (pint,) = PintAdapter().parse(fixture("pint.json"), "", 1)
    assert (pint.rule_id, pint.path, pint.severity) == ("pint.style", "app/Cart.php", Severity.LOW)


def test_phpstan_internal_errors_are_findings() -> None:
    out = json.dumps(
        {"totals": {}, "files": {}, "errors": ["Ignored error pattern was not matched"]}
    )
    (f,) = PHPStanAdapter().parse(out, "", 1)
    assert f.rule_id == "phpstan.error" and f.severity is Severity.HIGH


def test_mypy_parser() -> None:
    findings = MypyAdapter().parse(fixture("mypy.txt"), "", 1)
    assert [(f.rule_id, f.path) for f in findings] == [
        ("arg-type", "pkg/__init__.py"),
        ("no-untyped-def", "pkg/__init__.py"),
    ]


def test_osv_parser_on_documented_shape() -> None:
    out = json.dumps({"results": [{"source": {"path": "/src/composer.lock", "type": "lockfile"},
        "packages": [{"package": {"name": "guzzlehttp/psr7", "version": "1.8.0",
                                  "ecosystem": "Packagist"},
                      "vulnerabilities": [{"id": "GHSA-wxmh-65f7-jcvw",
                                           "summary": "Improper header",
                                           "database_specific": {"severity": "HIGH"}}],
                      "groups": [{"ids": ["GHSA-wxmh-65f7-jcvw"]}]}]}]})  # fmt: skip
    findings = OSVScannerAdapter().parse(out, target="/src")
    assert [(f.rule_id, f.path) for f in findings] == [("GHSA-wxmh-65f7-jcvw", "composer.lock")]
    assert "guzzlehttp/psr7" in findings[0].title
    assert OSVScannerAdapter().parse("", target="/src") == []


def test_versions() -> None:
    assert GoTestAdapter().parse_version("go version go1.24.3 linux/amd64") == "1.24.3"
    assert CargoTestAdapter().parse_version("cargo 1.87.0 (99624be96 2025-05-06)") == "1.87.0"
    assert MypyAdapter().parse_version("mypy 1.15.0 (compiled: yes)") == "1.15.0"


# -------------------------------------------------------------------- plans --
def test_plans_skip_with_reason_when_not_applicable(tmp_path: Path) -> None:
    for adapter in (PHPStanAdapter(), PintAdapter(), ESLintAdapter(), TypeScriptAdapter(),
                    MypyAdapter(), GoTestAdapter(), GolangciLintAdapter(), CargoTestAdapter(),
                    ClippyAdapter()):  # fmt: skip
        reason = adapter.plan(tmp_path)
        assert isinstance(reason, str) and reason, adapter.spec.name
    unconfigured = project(tmp_path / "py", {"pyproject.toml": "[project]\nname='x'\n"})
    assert "not configured" in str(MypyAdapter().plan(unconfigured))


def test_laravel_plans_install_vendor_when_missing(tmp_path: Path) -> None:
    composer = {"require-dev": {"phpstan/phpstan": "^2", "laravel/pint": "^1"}}
    root = project(tmp_path, {"composer.json": json.dumps(composer), "composer.lock": "{}",
                              "app/Cart.php": "<?php\n"})  # fmt: skip
    plan = PHPStanAdapter().plan(root)
    assert not isinstance(plan, str) and plan.install is not None
    assert plan.install.argv[:2] == ("composer", "install") and "--no-scripts" in plan.install.argv
    assert "packagist.org" in plan.install.hosts and plan.install.writes_project
    assert "--level=5" in plan.argv  # no phpstan.neon: a sensible default level
    project(root, {"vendor/bin/phpstan": "", "vendor/bin/pint": "", "phpstan.neon": ""})
    installed = PHPStanAdapter().plan(root)
    assert not isinstance(installed, str) and installed.install is None
    assert "--level=5" not in installed.argv


def test_node_plans(tmp_path: Path) -> None:
    pkg = {"devDependencies": {"eslint": "9", "typescript": "5"}}
    root = project(tmp_path, {"package.json": json.dumps(pkg), "package-lock.json": "{}",
                              "tsconfig.json": "{}", "eslint.config.js": ""})  # fmt: skip
    for adapter in (ESLintAdapter(), TypeScriptAdapter()):
        plan = adapter.plan(root)
        assert not isinstance(plan, str) and plan.install is not None
        assert plan.install.argv[:2] == ("npm", "ci") and "--ignore-scripts" in plan.install.argv
    no_dep = project(tmp_path / "b", {"package.json": "{}", "tsconfig.json": "{}"})
    assert "not a dependency" in str(TypeScriptAdapter().plan(no_dep))


def test_go_and_rust_plans_use_scratch_copies(tmp_path: Path) -> None:
    go = project(tmp_path / "go", {"go.mod": "module x\n\ngo 1.22\n"})
    plan = GoTestAdapter().plan(go)
    assert not isinstance(plan, str) and plan.writable and plan.install is None
    go_deps = project(tmp_path / "god", {"go.mod": "module x\n\nrequire github.com/a/b v1.0.0\n"})
    plan = GoTestAdapter().plan(go_deps)
    assert not isinstance(plan, str) and plan.install is not None
    assert plan.install.argv == ("go", "mod", "download")
    rust = project(
        tmp_path / "rs", {"Cargo.toml": "[package]\nname='x'\n[dependencies]\nserde='1'\n"}
    )
    clippy = ClippyAdapter().plan(rust)
    assert not isinstance(clippy, str) and clippy.toolchain == CLIPPY_CACHE
    assert "--offline" in clippy.argv and clippy.install is not None


# ------------------------------------------------------------------- runner --
@dataclass
class FakeExecutor:
    """Records commands; answers by the first argv element."""

    backend: ExecutionBackend = ExecutionBackend.DOCKER
    results: dict[str, ExecutionResult] = field(default_factory=dict)
    seen: list[CommandSpec] = field(default_factory=list)
    raise_error: bool = False

    def available(self) -> bool:
        return True

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        self.seen.append(spec)
        if self.raise_error:
            raise ExecutorError("image missing")
        key = " ".join(spec.argv[:2])
        default = ExecutionResult(self.backend, spec.argv, 0, "", "", 0.01)
        return self.results.get(key, self.results.get(spec.argv[0], default))


def result(code: int, out: str = "", err: str = "", **kw: bool) -> ExecutionResult:
    return ExecutionResult(ExecutionBackend.DOCKER, (), code, out, err, 0.1, **kw)


def runner(ex: FakeExecutor, root: Path, *, network: bool = True, allow: bool = True):  # type: ignore[no-untyped-def]
    return ProjectToolRunner(ex, root, network_allowed=lambda _h: allow, sandbox_network=network)


async def test_project_tools_never_run_on_the_host(tmp_path: Path) -> None:
    root = project(tmp_path, {"go.mod": "module x\n"})
    ex = FakeExecutor(backend=ExecutionBackend.LOCAL)
    run = await runner(ex, root).run(GoTestAdapter())
    assert run.skipped and "Docker sandbox only" in (run.error or "") and not ex.seen


async def test_read_only_mount_when_nothing_is_installed(tmp_path: Path) -> None:
    pkg = json.dumps({"devDependencies": {"eslint": "9"}})
    root = project(tmp_path / "p", {"package.json": pkg, "eslint.config.js": "",
                                    "node_modules/eslint/bin/eslint.js": ""})  # fmt: skip
    ex = FakeExecutor(results={"node": result(1, fixture("eslint.json"))})
    run = await runner(ex, root).run(ESLintAdapter())
    assert run.succeeded and len(run.findings) == 2 and not run.skipped
    scan = ex.seen[-1]
    work = next(m for m in scan.mounts if m.target == WORK)
    assert work.source == root.resolve() and work.read_only
    assert next(m for m in scan.mounts if m.target == DEPS).read_only is False
    assert all(not c.network for c in ex.seen)


async def test_install_runs_networked_on_a_scratch_copy(tmp_path: Path) -> None:
    composer = {"require-dev": {"phpstan/phpstan": "^2"}}
    root = project(tmp_path / "p", {"composer.json": json.dumps(composer), "app/a.php": "<?php"})
    ex = FakeExecutor(results={"php vendor/bin/phpstan": result(1, fixture("phpstan.json"))})
    run = await runner(ex, root).run(PHPStanAdapter())
    assert run.succeeded and len(run.findings) == 2
    install, *rest = ex.seen
    assert install.argv[0] == "composer" and install.network
    assert install.egress == COMPOSER_HOSTS  # only the registry hosts (B-205)
    assert all(not c.network for c in rest)  # only the install step is networked
    work = next(m for m in install.mounts if m.target == WORK)
    assert work.source != root.resolve() and not work.read_only
    assert not (root / "vendor").exists()  # the repository is never written


async def test_install_blocked_by_network_settings_or_policy(tmp_path: Path) -> None:
    root = project(tmp_path, {"Cargo.toml": "[package]\nname='x'\n[dependencies]\nserde='1'\n"})
    offline = await runner(FakeExecutor(), root, network=False).run(CargoTestAdapter())
    assert offline.skipped and "network is 'none'" in (offline.error or "")
    denied = await runner(FakeExecutor(), root, allow=False).run(CargoTestAdapter())
    assert denied.skipped and "index.crates.io" in (denied.error or "")


async def test_failures_are_fail_closed(tmp_path: Path) -> None:
    root = project(tmp_path, {"go.mod": "module x\n\nrequire github.com/a/b v1.0.0\n"})
    install_fail = FakeExecutor(results={"go mod": result(1, err="dial tcp: refused")})
    run = await runner(install_fail, root).run(GoTestAdapter())
    assert not run.succeeded and not run.skipped
    assert "dependency install failed" in (run.error or "") and "refused" in (run.error or "")
    crash = FakeExecutor(results={"go test": result(2, err="panic: boom")})
    assert "exit code 2" in ((await runner(crash, root).run(GoTestAdapter())).error or "")
    slow = FakeExecutor(results={"go test": result(0, timed_out=True)})
    assert (await runner(slow, root).run(GoTestAdapter())).error == "timed out"
    huge = FakeExecutor(results={"go test": result(0, stdout_truncated=True)})
    assert "size limit" in ((await runner(huge, root).run(GoTestAdapter())).error or "")
    garbage = FakeExecutor(results={"golangci-lint": result(1, "{not json")})
    bad = await runner(garbage, root).run(GolangciLintAdapter())
    assert "could not parse output" in (bad.error or "")
    broken = await runner(FakeExecutor(raise_error=True), root).run(GoTestAdapter())
    assert broken.error == "image missing" and not broken.succeeded


async def test_go_test_run_reports_version_and_coverage(tmp_path: Path) -> None:
    root = project(tmp_path, {"go.mod": "module x\n"})
    version = result(0, "go version go1.24.3 linux/amd64")
    ex = FakeExecutor(results={"go test": result(1, fixture("go-test.jsonl")),
                               "go version": version})  # fmt: skip
    run = await runner(ex, root).run(GoTestAdapter())
    assert run.succeeded and run.version == "1.24.3" and run.coverage == 100.0
    assert run.category is ToolCategory.TESTS and len(run.findings) == 1
    assert ToolRun.from_dict(run.to_dict()).coverage == 100.0


async def test_toolchain_must_be_prepared(tmp_path: Path, isolated_cache: Path) -> None:
    root = project(tmp_path / "rs", {"Cargo.toml": "[package]\nname='x'\n"})
    missing = await runner(FakeExecutor(), root).run(ClippyAdapter())
    assert missing.skipped and "tools pull -t clippy" in (missing.error or "")
    tool_cache(CLIPPY_CACHE).mkdir(parents=True)
    ex = FakeExecutor(results={"cargo clippy": result(0, fixture("clippy.jsonl"))})
    run = await runner(ex, root).run(ClippyAdapter())
    assert run.succeeded and len(run.findings) == 2
    assert any(m.target == "/toolchain" for m in ex.seen[-1].mounts)


# ------------------------------------------------------------ offline data --
def test_cache_location(isolated_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cache_root() == isolated_cache and tool_cache("trivy") == isolated_cache / "trivy"
    for bad in ("", "../x", ".hidden"):
        with pytest.raises(ValueError):
            tool_cache(bad)
    monkeypatch.delenv(ENV)
    assert cache_root() == Path.home() / ".cache" / "ai-coding"


def test_prepare_bookkeeping() -> None:
    assert needs_prepare(TrivyAdapter()) and needs_prepare(ClippyAdapter())
    assert not needs_prepare(GoTestAdapter()) and cache_name(GoTestAdapter()) is None
    assert cache_name(ClippyAdapter()) == CLIPPY_CACHE
    assert is_prepared(GoTestAdapter())  # nothing to prepare
    assert not is_prepared(TrivyAdapter()) and not is_prepared(ClippyAdapter())


class _WritesDB(FakeExecutor):
    async def run(self, spec: CommandSpec) -> ExecutionResult:
        cache = next(m.source for m in spec.mounts if m.target == "/cache")
        (cache / "db").mkdir(parents=True, exist_ok=True)
        (cache / "db" / "trivy.db").write_bytes(b"x")
        return await super().run(spec)


async def test_prepare_trivy_db_is_networked_and_verified() -> None:
    ex = _WritesDB()
    await prepare(TrivyAdapter(), ex)
    spec, fix_modes = ex.seen
    assert fix_modes.argv == readable_argv(fix_modes.mounts[0].target) and not fix_modes.network
    assert spec.network and spec.argv[0] == "trivy" and is_prepared(TrivyAdapter())
    assert spec.egress is None  # `tools pull` is an explicit operator download
    with pytest.raises(RuntimeError, match="preparation failed"):
        await prepare(OSVScannerAdapter(), FakeExecutor(results={"osv-scanner": result(1)}))


async def test_prepare_toolchain_mounts_toolchain_dir() -> None:
    ex = FakeExecutor()
    with pytest.raises(RuntimeError):  # nothing was installed by the fake
        await prepare(ClippyAdapter(), ex)
    spec, fix_modes = ex.seen
    assert fix_modes.argv == readable_argv(fix_modes.mounts[0].target) and not fix_modes.network
    assert spec.network and spec.mounts[0].target == "/toolchain"
    assert spec.env.get("RUSTUP_HOME", "").startswith("/toolchain")


async def test_sandbox_entry_starts_tools_whose_image_lacks_path(tmp_path: Path) -> None:
    from ai_coding.tools.runner import ToolRunner, sandbox_entry  # noqa: PLC0415

    assert sandbox_entry(TrivyAdapter()) == ("trivy",)
    entry = sandbox_entry(OSVScannerAdapter())
    assert entry[:2] == ("sh", "-c") and "/go/osv-scanner" in entry[2]
    osv_cache = tool_cache("osv-scanner") / "osv-scanner" / "npm"
    osv_cache.mkdir(parents=True)
    (osv_cache / "all.zip").write_bytes(b"zip")
    ex = FakeExecutor(results={"sh": result(0, json.dumps({"results": []}))})
    run = await ToolRunner(ex, tmp_path).run(OSVScannerAdapter())
    assert run.succeeded, run.error
    assert all(spec.argv[:4] == entry for spec in ex.seen)  # version and scan
    ex = FakeExecutor()
    await prepare(OSVScannerAdapter(), ex)  # the database zip above makes it "prepared"
    assert ex.seen[0].argv[:4] == entry and "--download-offline-databases" in ex.seen[0].argv


def test_failure_reason_keeps_the_panic_line() -> None:
    from ai_coding.tools.prepare import failure_reason  # noqa: PLC0415

    trace = "panic: runtime error: invalid memory address\n\ngoroutine 1 [running]:\n" + (
        "github.com/x/y.f()\n\t/src/y.go:1 +0x1\n" * 50) + "main.main()\n"  # fmt: skip
    reason = failure_reason(trace)
    assert reason.startswith("panic: runtime error: invalid memory address")
    assert reason.endswith("main.main()") and len(reason) <= 600
    assert failure_reason("a\nb\nc\nd") == "b | c | d"


class _Writer:
    """A tool that writes into a copy of the tree (Playwright: test-results/, caches)."""

    spec = ToolSpec(name="writer", category=ToolCategory.TESTS, binary="node",
                 image="img@sha256:" + "a" * 64)  # fmt: skip

    def plan(self, root: Path) -> Invocation:
        return Invocation(argv=("node", "x"), writable=True)

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0})

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Any]:
        return []

    def parse_version(self, output: str) -> str | None:
        return None


@dataclass
class _InspectingExecutor(FakeExecutor):
    """Looks at the work tree while the command 'runs' (it is removed afterwards)."""

    snapshot: dict[str, Any] = field(default_factory=dict)

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        modules = spec.cwd / "node_modules"
        entries = list(modules.rglob("*"))
        self.snapshot = {
            "files": sorted(str(p.relative_to(modules)) for p in entries),
            "links": {
                str(p.relative_to(modules)): str(p.readlink()) for p in entries if p.is_symlink()
            },
            "writable": os.access(modules, os.W_OK)
            and not any(m.target.startswith("/work/node_modules") for m in spec.mounts),
        }
        return await super().run(spec)


async def test_writable_copy_keeps_installed_dependencies_writable(tmp_path: Path) -> None:
    """A tool that writes into a copy of the tree still sees the project's installed
    node_modules — copied, so it can write caches there (node_modules/.vite, .cache),
    and never bind-mounted from the host."""
    modules = tmp_path / "node_modules"
    (modules / "pkg").mkdir(parents=True)
    (modules / "pkg" / "cli.js").write_text("1\n", encoding="utf-8")
    (modules / ".bin").mkdir()
    (modules / ".bin" / "pkg").symlink_to("../pkg/cli.js")
    (tmp_path / "app.js").write_text("1\n", encoding="utf-8")

    ex = _InspectingExecutor()
    run = await runner(ex, tmp_path).run(_Writer())
    assert run.succeeded, run.error
    assert "pkg/cli.js" in ex.snapshot["files"]
    assert ex.snapshot["links"] == {".bin/pkg": "../pkg/cli.js"}  # in-tree link kept
    assert ex.snapshot["writable"]


async def test_dependency_links_never_lead_out_of_the_project(tmp_path: Path) -> None:
    """A repository can commit symlinks; none of them may bring host files into the
    sandbox (a bind mount follows them: node_modules -> ~/.ssh)."""
    secret = tmp_path / "host-secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("PRIVATE\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.js").write_text("1\n", encoding="utf-8")
    (repo / "vendor").symlink_to(secret)  # the whole tree is a link out
    modules = repo / "node_modules"
    (modules / "ok").mkdir(parents=True)
    (modules / "abs").symlink_to(secret)  # absolute link out
    (modules / "rel").symlink_to("../../host-secret")  # relative link out
    (modules / "inside").symlink_to("../app.js")  # relative link inside the project

    ex = _InspectingExecutor()
    run = await runner(ex, repo).run(_Writer())
    assert run.succeeded, run.error
    assert ex.snapshot["links"] == {"inside": "../app.js"}
    assert not any("id_rsa" in f for f in ex.snapshot["files"])
    (spec,) = ex.seen
    sources = [m.source.resolve() for m in spec.mounts]
    assert not any(src == secret or secret in src.parents for src in sources)
    assert all(m.target in {"/work", "/deps"} for m in spec.mounts)
