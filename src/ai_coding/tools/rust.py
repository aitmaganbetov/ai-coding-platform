"""Rust project tools: ``cargo test`` and Clippy (ADR-0027).

Crates are fetched into ``/deps`` (networked, policy-gated) when the project has
dependencies; builds run ``--offline`` on a scratch copy with the target directory in
``/deps``. The official Rust images ship without Clippy, so ``ai-coding tools pull``
installs a pinned toolchain with Clippy into the platform cache, mounted at
``/toolchain`` during scans.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from ai_coding.domain.enums import Severity, Stack
from ai_coding.tools.base import Finding, ToolCategory, ToolSpec, fingerprint
from ai_coding.tools.project import Install, Invocation

RUST_IMAGE = (  # rust 1.87-slim (Docker Hub via mirror.gcr.io)
    "mirror.gcr.io/library/rust@sha256:437507c3e719e4f968033b88d851ffa9f5aceeb2dcc2482cc6cb7647811a55eb"
)
RUST_TOOLCHAIN = "1.87.0"
CLIPPY_CACHE = "rust-clippy"
CRATES_HOSTS = ("index.crates.io", "static.crates.io")
_ENV = {"CARGO_HOME": "/deps/cargo", "CARGO_TARGET_DIR": "/deps/target",
        "CARGO_TERM_COLOR": "never", "CARGO_NET_OFFLINE": "false"}  # fmt: skip
_FAILED = re.compile(r"^test (?P<name>\S+) \.\.\. FAILED$", re.M)
_DEPS = re.compile(r"^\[(?:dev-|build-)?dependencies\]\s*\n\s*[A-Za-z0-9_-]+\s*=", re.M)


def _cargo_plan(root: Path) -> tuple[dict[str, str], Install | None] | str:
    manifest = root / "Cargo.toml"
    if not manifest.is_file():
        return "no Cargo.toml"
    text = manifest.read_text(encoding="utf-8", errors="replace")
    if (root / "vendor").is_dir() or not _DEPS.search(text):
        return dict(_ENV), None
    return dict(_ENV), Install(argv=("cargo", "fetch"), hosts=CRATES_HOSTS)


class CargoTestAdapter:
    """``cargo test --offline --no-fail-fast``."""

    spec = ToolSpec(name="cargo-test", category=ToolCategory.TESTS, binary="cargo",
                    image=RUST_IMAGE, stacks=frozenset({Stack.RUST}),
                    executes_project_code=True,
                    homepage="https://doc.rust-lang.org/cargo/")  # fmt: skip

    def plan(self, root: Path) -> Invocation | str:
        planned = _cargo_plan(root)
        if isinstance(planned, str):
            return planned
        env, install = planned
        return Invocation(argv=("cargo", "test", "--offline", "--no-fail-fast"), env=env,
                          install=install, writable=True,
                          version_argv=("cargo", "--version"))  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 101})  # 101: test or build failure (reported as findings)

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"cargo (\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        text = stdout + "\n" + stderr
        findings = []
        for m in _FAILED.finditer(text):
            name = m["name"]
            pattern = rf"thread '{re.escape(name)}' panicked at (?P<loc>[^\n]+)\n(?P<msg>[^\n]*)"
            panic = re.search(pattern, text)
            path, line = None, None
            if panic:
                loc = panic["loc"].rstrip(":")
                parts = loc.split(":")
                path = parts[0]
                line = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            message = f"{panic['msg']} ({panic['loc']})" if panic else "test failed"
            findings.append(Finding(
                tool="cargo-test", rule_id="cargo-test-failure", severity=Severity.HIGH,
                title=f"{name} failed"[:200], message=message, path=path, line=line,
                fingerprint=fingerprint("cargo-test", "cargo-test-failure", path, name),
            ))  # fmt: skip
        if "could not compile" in text and not findings:
            errors = [ln for ln in text.splitlines() if ln.startswith("error")][:5]
            findings.append(Finding(
                tool="cargo-test", rule_id="cargo-build-failure", severity=Severity.HIGH,
                title="crate does not compile", message="\n".join(errors)[:1500], path=None,
                line=None, fingerprint=fingerprint("cargo-test", "cargo-build-failure", None,
                                                   "\n".join(errors)),
            ))  # fmt: skip
        if exit_code != 0 and not findings:
            raise ValueError(f"cargo test failed: {text[-300:]}")
        return findings


class ClippyAdapter:
    """``cargo clippy --message-format=json`` with a prepared toolchain."""

    spec = ToolSpec(name="clippy", category=ToolCategory.LINT, binary="cargo", image=RUST_IMAGE,
                    stacks=frozenset({Stack.RUST}), executes_project_code=True,
                    homepage="https://doc.rust-lang.org/clippy/")  # fmt: skip
    toolchain_cache = CLIPPY_CACHE

    def prepare(self) -> tuple[Sequence[str], Mapping[str, str]]:
        """Networked toolchain install (``ai-coding tools pull``)."""
        return (["rustup", "toolchain", "install", RUST_TOOLCHAIN, "--profile", "minimal",
                 "--component", "clippy", "--no-self-update"],
                {"RUSTUP_HOME": "/toolchain/rustup", "CARGO_HOME": "/toolchain/cargo"})  # fmt: skip

    def plan(self, root: Path) -> Invocation | str:
        planned = _cargo_plan(root)
        if isinstance(planned, str):
            return planned
        env, install = planned
        env |= {"RUSTUP_HOME": "/toolchain/rustup", "RUSTUP_TOOLCHAIN": RUST_TOOLCHAIN}
        return Invocation(argv=("cargo", "clippy", "--offline", "--message-format=json",
                                "--all-targets"), env=env, install=install, writable=True,
                          toolchain=CLIPPY_CACHE,
                          version_argv=("cargo", "clippy", "--version"))  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 101})  # 101: clippy errors (deny lints)

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"clippy (\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        seen: set[str] = set()
        findings = []
        for line in stdout.splitlines():
            if not line.startswith("{"):
                continue
            data = json.loads(line)
            if data.get("reason") != "compiler-message":
                continue
            msg = data.get("message") or {}
            spans = [s for s in msg.get("spans") or [] if s.get("is_primary")]
            if not spans or msg.get("level") not in {"warning", "error"}:
                continue
            code = str((msg.get("code") or {}).get("code") or "rustc")
            path, line_no = str(spans[0]["file_name"]), int(spans[0]["line_start"])
            text = str(msg.get("message", ""))
            fp = fingerprint("clippy", code, path, text)
            if fp in seen:
                continue  # the same diagnostic for lib and lib-test targets
            seen.add(fp)
            findings.append(Finding(
                tool="clippy", rule_id=code,
                severity=Severity.MEDIUM if msg.get("level") == "error" else Severity.LOW,
                title=f"{code}: {text}"[:200], message=text, path=path, line=line_no,
                fingerprint=fp,
            ))  # fmt: skip
        if exit_code != 0 and not any(f.severity is Severity.MEDIUM for f in findings):
            raise ValueError(f"cargo clippy failed: {stderr[-300:]}")
        return findings
