"""Dependency upgrades (package managers in the sandbox) and MAINTAIN source handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ai_coding.domain.enums import ExecutionBackend
from ai_coding.executors.base import CommandSpec, ExecutionResult
from ai_coding.orchestrator.errors import PermanentError
from ai_coding.tools.deps import (
    DependencyUpdateError,
    Upgrade,
    apply_upgrades,
    command_for,
    ecosystem_of,
    hosts_for,
    pin_requirement,
)
from ai_coding.workflows.maintain import (
    next_item_id,
    pick_item,
    plan_version,
    render_plan,
    sources_from_report,
    upgrades_of,
)


@dataclass
class PackageManager:
    """Executor double: rewrites the version in the mounted manifest/lock files."""

    old: str
    new: str
    exit_code: int = 0
    stderr: str = ""
    backend: ExecutionBackend = ExecutionBackend.DOCKER
    specs: list[CommandSpec] = field(default_factory=list)

    def available(self) -> bool:
        return True

    async def run(self, spec: CommandSpec) -> ExecutionResult:
        self.specs.append(spec)
        work = next(m.source for m in spec.mounts if m.target == "/work")
        if self.exit_code == 0:
            for f in work.iterdir():
                f.write_text(f.read_text(encoding="utf-8").replace(self.old, self.new),
                             encoding="utf-8")  # fmt: skip
        return ExecutionResult(self.backend, spec.argv, self.exit_code, "", self.stderr, 0.1)


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def allow(_host: str) -> bool:
    return True


def test_upgrade_validation_and_commands() -> None:
    for bad in ({"package": "--evil", "version": "1"}, {"package": "a", "version": "1; rm"},
                {"package": "a", "version": "1", "directory": "../x"}):  # fmt: skip
        with pytest.raises(ValueError):
            Upgrade("npm", **bad)
    npm = command_for(Upgrade("npm", "lodash", "4.17.21"))
    assert npm is not None and "--ignore-scripts" in npm.argv and "--package-lock-only" in npm.argv
    php = command_for(Upgrade("Packagist", "guzzlehttp/psr7", "2.1.1"))
    assert php is not None and {"--no-scripts", "--no-plugins", "--no-install"} <= set(php.argv)
    go = command_for(Upgrade("Go", "golang.org/x/net", "0.38.0"))
    assert go is not None and go.argv == ("go", "get", "golang.org/x/net@v0.38.0")
    rust = command_for(Upgrade("crates.io", "regex", "1.5.5"))
    assert rust is not None and rust.argv[-3:] == ("regex", "--precise", "1.5.5")
    assert command_for(Upgrade("PyPI", "django", "4.2.16")) is None  # text edit, no network
    with pytest.raises(DependencyUpdateError, match="not supported"):
        command_for(Upgrade("Maven", "x", "1"))
    assert hosts_for([Upgrade("npm", "a", "1"), Upgrade("npm", "b", "2")]) == ["registry.npmjs.org"]
    assert ecosystem_of("web/package-lock.json") == "npm" and ecosystem_of("x.txt") is None


def test_pin_requirement() -> None:
    text = "Django==3.2.0  # web\ndjango-extensions==1\nflask[async]>=2.0,<3 ; python_version>'3'\n"
    out = pin_requirement(text, "django", "3.2.25")
    assert out.splitlines()[:2] == ["Django==3.2.25  # web", "django-extensions==1"]
    assert "flask[async]==2.3.3 ; python_version>'3'" in pin_requirement(text, "flask", "2.3.3")
    with pytest.raises(DependencyUpdateError, match="not listed"):
        pin_requirement(text, "requests", "2.32.0")


async def test_apply_upgrades_runs_the_package_manager_on_a_copy(tmp_path: Path) -> None:
    root = write(tmp_path / "p", {"web/package.json": '{"lodash": "4.17.15"}\n',
                                  "web/package-lock.json": '{"v": "4.17.15"}\n',
                                  "requirements.txt": "django==3.2.0\n"})  # fmt: skip
    pm = PackageManager("4.17.15", "4.17.21")
    changes = await apply_upgrades(pm, root, [Upgrade("npm", "lodash", "4.17.21", "web"),
                                              Upgrade("PyPI", "django", "3.2.25")],
                                   network_allowed=allow, sandbox_network=True)  # fmt: skip
    assert [(c["path"], c["content"]) for c in changes] == [
        ("requirements.txt", "django==3.2.25\n"),
        ("web/package-lock.json", '{"v": "4.17.21"}\n'),
        ("web/package.json", '{"lodash": "4.17.21"}\n'),
    ]
    (spec,) = pm.specs  # PyPI needed no command
    assert spec.network and spec.argv[:2] == ("npm", "install")
    work = next(m for m in spec.mounts if m.target == "/work")
    assert work.source != root / "web" and not work.read_only
    assert (root / "web" / "package-lock.json").read_text(encoding="utf-8") == '{"v": "4.17.15"}\n'


async def test_apply_upgrades_errors(tmp_path: Path) -> None:
    root = write(tmp_path, {"composer.json": "{}", "composer.lock": '{"v": "1.8.0"}'})
    up = [Upgrade("Packagist", "guzzlehttp/psr7", "1.8.4")]
    with pytest.raises(DependencyUpdateError, match="network is 'none'"):
        await apply_upgrades(PackageManager("", ""), root, up, network_allowed=allow,
                             sandbox_network=False)  # fmt: skip
    with pytest.raises(DependencyUpdateError, match=r"egress to repo\.packagist\.org"):
        await apply_upgrades(PackageManager("", ""), root, up,
                             network_allowed=lambda h: h != "repo.packagist.org",
                             sandbox_network=True)  # fmt: skip
    conflict = PackageManager("1.8.0", "1.8.4", exit_code=2, stderr=(
        "Loading composer repositories\n  - guzzlehttp/guzzle 7.9 requires psr7 ^2.7 -> "
        "it conflicts with your root composer.json require (1.8.4).\nInstallation failed, "
        "reverting ./composer.json\n"))  # fmt: skip
    with pytest.raises(DependencyUpdateError, match="conflicts with your root"):
        await apply_upgrades(conflict, root, up, network_allowed=allow, sandbox_network=True)
    with pytest.raises(DependencyUpdateError, match="changed nothing"):
        await apply_upgrades(PackageManager("x", "x"), root, up, network_allowed=allow,
                             sandbox_network=True)  # fmt: skip


# ------------------------------------------------------------------ maintain --
def finding(tool: str, rule: str, path: str, sev: str = "high", **ev: object) -> dict:  # type: ignore[type-arg]
    return {"tool": tool, "rule_id": rule, "severity": sev, "path": path, "line": None,
            "title": rule, "fingerprint": (rule + path).ljust(64, "0")[:64], "status": "new",
            "evidence": ev}  # fmt: skip


def test_sources_from_report_groups_vulnerable_dependencies() -> None:
    report = {"findings": [
        finding("trivy", "CVE-1", "package-lock.json", package="lodash", installed="4.17.15",
                fixed="4.17.19, 4.17.21"),
        finding("osv-scanner", "GHSA-1", "package-lock.json", "medium", package="lodash",
                installed="4.17.15", fixed="4.17.21", ecosystem="npm"),
        finding("trivy", "CVE-2", "composer.lock", package="guzzlehttp/psr7", installed="1.8.0",
                fixed=None),
        finding("semgrep", "php-unserialize", "app/A.php"),
        {**finding("semgrep", "old", "app/B.php"), "status": "baselined"},
    ]}  # fmt: skip
    findings, deps = sources_from_report(report)
    assert [f["rule_id"] for f in findings] == ["php-unserialize"]
    assert findings[0]["id"] == report["findings"][3]["fingerprint"][:12]
    by_pkg = {d["package"]: d for d in deps}
    lodash = by_pkg["lodash"]
    assert lodash["fixed_versions"] == ["4.17.19", "4.17.21"]
    assert lodash["advisories"] == ["CVE-1", "GHSA-1"] and lodash["ecosystem"] == "npm"
    assert by_pkg["guzzlehttp/psr7"]["ecosystem"] == "Packagist"
    assert by_pkg["guzzlehttp/psr7"]["fixed_versions"] == []
    assert sorted(d["id"] for d in deps) == ["D1", "D2"]


PLAN = {"summary": "s", "deferred": [{"source": "#9", "reason": "needs info"}], "items": [
    {"id": "M1", "title": "a", "kind": "bug", "priority": "p2", "sources": ["#1"], "plan": "p",
     "files": ["a.py"], "risk": "low", "regression_test": {"file": "tests/test_a.py",
                                                           "description": "d"}},
    {"id": "M2", "title": "b", "kind": "dependency", "priority": "p0", "sources": ["D1"],
     "plan": "p", "files": ["web/package.json"], "risk": "low",
     "upgrades": [{"source": "D1", "to_version": "4.17.21"}],
     "regression_test": {"file": "tests/test_b.py", "description": "d"}},
]}  # fmt: skip


def test_plan_helpers() -> None:
    assert pick_item(PLAN, None)["id"] == "M2" and pick_item(PLAN, "M1")["id"] == "M1"
    with pytest.raises(PermanentError, match="not in the plan"):
        pick_item(PLAN, "M7")
    with pytest.raises(PermanentError, match="no items"):
        pick_item({"items": []}, None)
    assert next_item_id(PLAN, ["M2"]) == "M1" and next_item_id(PLAN, ["M1", "M2"]) is None
    assert plan_version(PLAN) == plan_version(dict(PLAN)) != plan_version({**PLAN, "summary": "x"})
    md = render_plan(PLAN, "m-1")
    assert md.index("### M2") < md.index("### M1") and "Upgrade: D1 → 4.17.21" in md
    assert "- #9: needs info" in md
    sources = {"dependencies": [{"id": "D1", "ecosystem": "npm", "package": "lodash",
                                 "path": "web/package-lock.json",
                                 "fixed_versions": ["4.17.21"]}]}  # fmt: skip
    (up,) = upgrades_of(PLAN["items"][1], sources)
    assert (up.ecosystem, up.package, up.version, up.directory) == (
        "npm",
        "lodash",
        "4.17.21",
        "web",
    )
    assert upgrades_of(PLAN["items"][0], sources) == []
    with pytest.raises(PermanentError, match="not backed"):
        upgrades_of(PLAN["items"][1], {"dependencies": []})
