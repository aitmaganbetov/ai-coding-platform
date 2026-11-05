"""Deterministic dependency upgrades for MAINTAIN (B-610).

A vulnerable dependency is upgraded by the ecosystem's own package manager, so the
lock file (resolution, integrity hashes) is real — never written by a model. The
command runs in the Docker sandbox on a scratch copy of the manifest directory; it is
the only networked step and needs ``execution.docker.network: bridge`` plus an
egress-policy allow for the registry hosts. ``requirements.txt`` pins are rewritten
without network. Lifecycle scripts are disabled (``--ignore-scripts``,
``--no-scripts``/``--no-plugins``).

The result is a list of full-file changes (``modify``) relative to the project root,
which the implement loop takes as its starting point.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_coding.executors.base import CommandSpec, Executor, Mount
from ai_coding.tools.golang import GO_HOSTS, GO_IMAGE
from ai_coding.tools.php import COMPOSER_HOSTS, COMPOSER_IMAGE
from ai_coding.tools.rust import CRATES_HOSTS, RUST_IMAGE
from ai_coding.tools.scratch import Scratch, sandbox_scratch
from ai_coding.tools.testrun import NODE_IMAGE, NPM_HOSTS, open_permissions

WORK = "/work"
DEPS = "/deps"
_ERROR_CHARS = 400
_REASON = re.compile(r"conflict|not found|no matching|could not|cannot|ERR!|error", re.I)
_NAME = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._/@-]{0,213}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_~-]{0,63}$")

# lock/manifest file name → ecosystem
LOCK_ECOSYSTEM: dict[str, str] = {
    "package-lock.json": "npm",
    "composer.lock": "Packagist",
    "go.mod": "Go",
    "go.sum": "Go",
    "Cargo.lock": "crates.io",
    "requirements.txt": "PyPI",
}
_TOUCHED: dict[str, tuple[str, ...]] = {
    "npm": ("package.json", "package-lock.json"),
    "Packagist": ("composer.json", "composer.lock"),
    "Go": ("go.mod", "go.sum"),
    "crates.io": ("Cargo.toml", "Cargo.lock"),
    "PyPI": ("requirements.txt",),
}


class DependencyUpdateError(RuntimeError):
    """An upgrade could not be applied (with the reason)."""


@dataclass(frozen=True, slots=True)
class Upgrade:
    """Upgrade ``package`` to ``version`` in the project at ``directory`` (relative)."""

    ecosystem: str
    package: str
    version: str
    directory: str = "."

    def __post_init__(self) -> None:
        if not _NAME.match(self.package) or self.package.startswith("-"):
            raise ValueError(f"invalid package name {self.package!r}")
        if not _VERSION.match(self.version):
            raise ValueError(f"invalid version {self.version!r}")
        parts = Path(self.directory).parts
        if Path(self.directory).is_absolute() or ".." in parts:
            raise ValueError(f"invalid directory {self.directory!r}")


@dataclass(frozen=True, slots=True)
class _Command:
    image: str
    argv: tuple[str, ...]
    hosts: tuple[str, ...]
    env: Mapping[str, str]


def ecosystem_of(lock_path: str) -> str | None:
    """Ecosystem of a lock/manifest path reported by a scanner."""
    return LOCK_ECOSYSTEM.get(Path(lock_path).name)


def command_for(up: Upgrade) -> _Command | None:
    """Sandbox command for ``up``; ``None`` when no network command is needed."""
    if up.ecosystem == "npm":
        return _Command(NODE_IMAGE, ("npm", "install", f"{up.package}@{up.version}",
                                     "--package-lock-only", "--ignore-scripts", "--no-audit",
                                     "--no-fund", "--save-exact"),
                        NPM_HOSTS, {"npm_config_cache": f"{DEPS}/npm", "HOME": DEPS})  # fmt: skip
    if up.ecosystem == "Packagist":
        return _Command(COMPOSER_IMAGE, ("composer", "require", f"{up.package}:{up.version}",
                                         "--no-install", "--no-scripts", "--no-plugins",
                                         "--no-interaction", "--no-progress"),
                        COMPOSER_HOSTS, {"COMPOSER_HOME": f"{DEPS}/composer",
                                         "HOME": DEPS})  # fmt: skip
    if up.ecosystem == "Go":
        version = up.version if up.version.startswith("v") else f"v{up.version}"
        return _Command(GO_IMAGE, ("go", "get", f"{up.package}@{version}"), GO_HOSTS,
                        {"GOPATH": f"{DEPS}/gopath", "GOMODCACHE": f"{DEPS}/gopath/pkg/mod",
                         "GOCACHE": f"{DEPS}/cache", "GOFLAGS": "-mod=mod",
                         "HOME": DEPS})  # fmt: skip
    if up.ecosystem == "crates.io":
        return _Command(RUST_IMAGE, ("cargo", "update", "-p", up.package, "--precise",
                                     up.version),
                        CRATES_HOSTS, {"CARGO_HOME": f"{DEPS}/cargo", "HOME": DEPS})  # fmt: skip
    if up.ecosystem == "PyPI":
        return None
    raise DependencyUpdateError(f"upgrades for {up.ecosystem} are not supported")


def hosts_for(upgrades: Sequence[Upgrade]) -> list[str]:
    """Registry hosts the upgrades need (for policy checks and approval texts)."""
    hosts: list[str] = []
    for up in upgrades:
        cmd = command_for(up)
        hosts += [h for h in (cmd.hosts if cmd else ()) if h not in hosts]
    return hosts


def pin_requirement(text: str, package: str, version: str) -> str:
    """``requirements.txt`` with ``package`` pinned to ``version`` (other lines intact)."""
    pattern = re.compile(rf"^(?P<name>{re.escape(package)})(?P<extras>\[[^\]]*\])?"
                         r"\s*(?:[=<>!~]=?[^;#\s]*(?:\s*,\s*[=<>!~]=?[^;#\s]*)*)?"
                         r"(?P<rest>\s*(?:;[^#]*)?(?:#.*)?)$",
                         re.IGNORECASE | re.MULTILINE)  # fmt: skip
    new, count = pattern.subn(lambda m: f"{m['name']}{m['extras'] or ''}=={version}{m['rest']}",
                              text)  # fmt: skip
    if count == 0:
        raise DependencyUpdateError(f"{package} is not listed in requirements.txt")
    return new


async def apply_upgrades(
    executor: Executor,
    project: Path,
    upgrades: Sequence[Upgrade],
    *,
    network_allowed: Callable[[str], bool],
    sandbox_network: bool,
    timeout_seconds: float = 900.0,
) -> list[dict[str, Any]]:
    """Run the upgrades on a scratch copy; full-file changes of the touched files."""
    hosts = hosts_for(upgrades)
    if hosts and not sandbox_network:
        raise DependencyUpdateError(
            "dependency upgrades need registry access, but execution.docker.network is 'none'"
        )
    denied = [h for h in hosts if not network_allowed(h)]
    if denied:
        raise DependencyUpdateError(
            f"dependency upgrades need egress to {', '.join(denied)} (security policy)"
        )
    changes: dict[str, str] = {}
    async with sandbox_scratch(executor, "ai-coding-deps-") as scratch:
        for up in upgrades:
            await _upgrade_one(executor, project, scratch, up, changes=changes,
                               timeout=timeout_seconds)  # fmt: skip
    if not changes:
        raise DependencyUpdateError("the package manager changed nothing (already at that "
                                    "version, or the constraint forbids it)")  # fmt: skip
    return [{"path": p, "action": "modify", "content": c} for p, c in sorted(changes.items())]


def _current(project: Path, rel: str, changes: Mapping[str, str]) -> str | None:
    """File content including earlier upgrades of this run."""
    if rel in changes:
        return changes[rel]
    path = project / rel
    return path.read_text(encoding="utf-8") if path.is_file() else None


async def _upgrade_one(
    executor: Executor,
    project: Path,
    scratch: Scratch,
    up: Upgrade,
    *,
    changes: dict[str, str],
    timeout: float,
) -> None:
    """Apply one upgrade on a fresh work directory; record the files it changed."""
    names = _TOUCHED[up.ecosystem]
    rels = {n: (Path(up.directory) / n).as_posix().removeprefix("./") for n in names}
    # A new directory per upgrade: the previous one may hold files of the sandbox user.
    work = Path(tempfile.mkdtemp(prefix="work-", dir=scratch.path))
    for name, rel in rels.items():  # only the manifest and lock files leave the project
        text = _current(project, rel, changes)
        if text is not None:
            (work / name).write_text(text, encoding="utf-8")
    if up.ecosystem == "PyPI":
        req = work / "requirements.txt"
        if not req.is_file():
            raise DependencyUpdateError("requirements.txt not found")
        text = req.read_text(encoding="utf-8")
        req.write_text(pin_requirement(text, up.package, up.version), encoding="utf-8")
    else:
        await _run(executor, work, scratch, up, timeout)
    for name, rel in rels.items():
        after = work / name
        if after.is_file():
            text = after.read_text(encoding="utf-8")
            if text != _current(project, rel, changes):
                changes[rel] = text


async def _run(
    executor: Executor, work: Path, scratch: Scratch, up: Upgrade, timeout: float
) -> None:
    cmd = command_for(up)
    if cmd is None:  # pragma: no cover - only PyPI, handled without a command
        raise DependencyUpdateError(f"no command for {up.ecosystem}")
    scratch.used(cmd.image)
    deps = scratch.path / "deps"
    deps.mkdir(exist_ok=True)
    open_permissions(work)
    open_permissions(scratch.path, recursive=False)
    open_permissions(deps, recursive=False)
    result = await executor.run(CommandSpec(
        argv=cmd.argv, cwd=work, image=cmd.image, workdir=WORK, network=True,
        egress=tuple(cmd.hosts), env=dict(cmd.env),  # only the registry (B-205)
        mounts=(Mount(work, WORK, read_only=False), Mount(deps, DEPS, read_only=False)),
        timeout_seconds=timeout, ok_exit_codes=frozenset(range(256)),
    ))  # fmt: skip
    if result.timed_out or result.exit_code != 0:
        lines = [ln.strip() for ln in (result.stderr + "\n" + result.stdout).splitlines()
                 if ln.strip()]  # fmt: skip
        key = [ln for ln in lines if _REASON.search(ln)]
        tail = " | ".join((key or lines)[-3:])
        raise DependencyUpdateError(
            f"{' '.join(cmd.argv[:2])} {up.package}@{up.version} failed "
            f"(exit {result.exit_code}): {tail[-_ERROR_CHARS:]}"
        )
