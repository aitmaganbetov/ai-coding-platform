"""Policy Engine: deterministic policy-as-code decisions (FR-11, ТЗ §12).

Every decision carries the rule that produced it and a reason, so that denials are
explainable and auditable. Defaults are deny: anything not explicitly allowed by
configuration is denied or requires a human approval.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from ai_coding.config.models import AICodingConfig
from ai_coding.domain.enums import (
    AgentName,
    ApprovalOperation,
    Mode,
    NetworkPolicy,
    ProviderName,
)

PROVIDER_HOSTS: dict[ProviderName, str] = {
    ProviderName.OPENAI: "api.openai.com",
    ProviderName.ANTHROPIC: "api.anthropic.com",
}
MUTATING_OPERATIONS: frozenset[ApprovalOperation] = frozenset(ApprovalOperation)


class Verdict(StrEnum):
    """Outcome of a policy evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class Decision:
    """A policy decision with provenance."""

    verdict: Verdict
    rule: str
    reason: str
    operation: ApprovalOperation | None = None

    @property
    def allowed(self) -> bool:
        """``True`` only for :attr:`Verdict.ALLOW`."""
        return self.verdict is Verdict.ALLOW


@dataclass(frozen=True, slots=True)
class PolicyOptions:
    """Per-run overrides that can only make policy stricter."""

    require_approval_all: bool = False  # CLI --require-approval
    granted: frozenset[ApprovalOperation] = field(default_factory=frozenset)


class PolicyEngine:
    """Evaluates actions against the project configuration."""

    def __init__(self, config: AICodingConfig, options: PolicyOptions | None = None) -> None:
        self._cfg = config
        self._opts = options or PolicyOptions()
        self._required = set(config.approvals.required_for)

    @property
    def digest(self) -> str:
        """SHA-256 of the effective policy (recorded in the audit trail)."""
        doc = {
            "config": self._cfg.model_dump(mode="json"),
            "require_approval_all": self._opts.require_approval_all,
        }
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()

    def with_options(self, options: PolicyOptions) -> PolicyEngine:
        """Same configuration, different per-run options."""
        return PolicyEngine(self._cfg, options)

    # ------------------------------------------------------------ helpers --
    def _approval(self, op: ApprovalOperation, rule: str, reason: str) -> Decision:
        if op in self._opts.granted:
            return Decision(Verdict.ALLOW, rule, f"{reason}; approval granted", op)
        return Decision(Verdict.REQUIRE_APPROVAL, rule, reason, op)

    def _needs_approval(self, op: ApprovalOperation) -> bool:
        return (
            op in ApprovalOperation.mandatory()
            or op in self._required
            or (self._opts.require_approval_all and op in MUTATING_OPERATIONS)
        )

    # -------------------------------------------------------------- rules --
    def operation(self, op: ApprovalOperation) -> Decision:
        """Generic guarded operation (merge, deploy, delete, migration, PR, …)."""
        if self._needs_approval(op):
            return self._approval(op, "approvals.required_for", f"'{op}' requires human approval")
        return Decision(Verdict.ALLOW, "approvals.required_for", f"'{op}' does not need approval")

    def agent(self, name: AgentName | str) -> Decision:
        """Whether an agent may be scheduled."""
        enabled = {a.value for a in self._cfg.agents.enabled}
        value = name.value if isinstance(name, AgentName) else name
        if value not in {a.value for a in AgentName}:
            return Decision(Verdict.ALLOW, "agents.builtin", f"'{value}' is a platform handler")
        if value in enabled:
            return Decision(Verdict.ALLOW, "agents.enabled", f"agent '{value}' is enabled")
        return Decision(Verdict.DENY, "agents.enabled", f"agent '{value}' is not enabled")

    def file_write(self, path: str) -> Decision:
        """Writing a repository file (agents work in a worktree; ТЗ §5)."""
        posix = PurePosixPath(path.replace("\\", "/"))
        if posix.is_absolute() or ".." in posix.parts:
            return Decision(Verdict.DENY, "fs.scope", f"path '{path}' escapes the project")
        if self._cfg.mode is Mode.AUDIT:
            return Decision(Verdict.DENY, "mode.audit", "AUDIT mode is read-only (FR-06)")
        for pattern in self._cfg.approvals.critical_paths:
            if _path_matches(str(posix), pattern):
                return self.operation_for_critical(str(posix), pattern)
        return Decision(Verdict.ALLOW, "fs.write", f"write to '{posix}' allowed")

    def operation_for_critical(self, path: str, pattern: str) -> Decision:
        """Critical file change → approval (always, even if not listed: ТЗ §5)."""
        op = ApprovalOperation.CRITICAL_FILE_CHANGE
        return self._approval(op, "approvals.critical_paths", f"'{path}' matches '{pattern}'")

    def command(self, executable: str, *, on_host: bool) -> Decision:
        """Running a command; host execution is deny-by-default (ТЗ §12)."""
        exe = PurePosixPath(executable.replace("\\", "/")).name
        if not on_host:
            return Decision(Verdict.ALLOW, "exec.sandbox", f"'{exe}' runs in the sandbox")
        if not self._cfg.execution.allow_host_commands:
            return Decision(
                Verdict.DENY, "execution.allow_host_commands", "host commands are disabled"
            )
        if exe in self._cfg.execution.command_allowlist:
            if self._opts.require_approval_all:
                return self._approval(
                    ApprovalOperation.HOST_COMMAND, "--require-approval", f"'{exe}' on host"
                )
            return Decision(Verdict.ALLOW, "execution.command_allowlist", f"'{exe}' allow-listed")
        if self._needs_approval(ApprovalOperation.HOST_COMMAND):
            return self._approval(
                ApprovalOperation.HOST_COMMAND,
                "execution.command_allowlist",
                f"'{exe}' is not allow-listed",
            )
        return Decision(Verdict.DENY, "execution.command_allowlist", f"'{exe}' is not allow-listed")

    def network(self, host: str) -> Decision:
        """Outbound connection to ``host`` (``host`` or ``host:port``)."""
        host = host.lower().strip()
        network = self._cfg.security.network
        if _is_loopback(host):
            return Decision(Verdict.ALLOW, "security.network", "loopback is not egress")
        if network is NetworkPolicy.LOCAL_ONLY:
            return Decision(Verdict.DENY, "security.network", "local-only forbids egress")
        if network is NetworkPolicy.OPEN:
            return Decision(Verdict.ALLOW, "security.network", "network is open")
        for pattern in self._cfg.security.egress_allowlist:
            if _host_matches(host, pattern.lower()):
                return Decision(
                    Verdict.ALLOW, "security.egress_allowlist", f"'{host}' matches '{pattern}'"
                )
        return Decision(
            Verdict.DENY, "security.egress_allowlist", f"'{host}' is not in the allowlist"
        )

    def external_llm(self, provider: ProviderName, *, base_url: str | None = None) -> Decision:
        """Sending code/context to an LLM provider (ТЗ §12, FR-18)."""
        if not self._cfg.ai.enabled:
            return Decision(Verdict.DENY, "ai.enabled", "AI is disabled")
        if provider not in self._cfg.ai.providers_in_use():
            return Decision(Verdict.DENY, "ai.provider", f"provider '{provider}' not configured")
        host = urlsplit(base_url).netloc if base_url else PROVIDER_HOSTS.get(provider)
        if host:
            net = self.network(host)
            if not net.allowed:
                return net
        if provider.is_local:
            return Decision(Verdict.ALLOW, "ai.provider", f"'{provider}' is a local provider")
        if self._needs_approval(ApprovalOperation.EXTERNAL_LLM):
            return self._approval(
                ApprovalOperation.EXTERNAL_LLM,
                "approvals.required_for",
                f"sending context to external provider '{provider}'",
            )
        return Decision(Verdict.ALLOW, "ai.provider", f"'{provider}' allowed by policy")


def _path_matches(path: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(path, pattern):
        return True
    # '**/x' should also match 'x' at the root.
    return pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])


def _host_matches(host: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    return host == pattern or host.split(":", 1)[0] == pattern


def _is_loopback(host: str) -> bool:
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name in {"localhost", "127.0.0.1", "::1", "[::1]"}
