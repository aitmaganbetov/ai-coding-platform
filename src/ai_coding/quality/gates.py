"""Quality Gate engine: PASS / FAIL / SKIPPED from real tool results only (ТЗ §10).

Rules (ADR-0012, ADR-0020):

* a gate's result is computed only from deterministic tool runs; AI output never
  influences it;
* fail closed: if a tool that a gate depends on failed to run, the gate FAILs;
* a gate with no applicable tool is SKIPPED with the reason; in ``strict`` mode a
  SKIPPED *blocking* gate makes the verdict FAIL;
* baselined findings (active owner/expiry) do not count; expired ones do.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from ai_coding.config.models import QualityGatesConfig
from ai_coding.domain.enums import GateResult
from ai_coding.quality.baseline import Baseline
from ai_coding.tools.base import Finding, ToolCategory, ToolRun

_MAX_EVIDENCE = 50


class GateName(StrEnum):
    """The six gates of ТЗ §10."""

    BUILD = "build"
    TESTS = "tests"
    STATIC = "static"
    SECURITY = "security"
    COVERAGE = "coverage"
    REVIEW = "review"


_GATE_CATEGORIES: dict[GateName, frozenset[ToolCategory]] = {
    GateName.BUILD: frozenset({ToolCategory.BUILD}),
    GateName.TESTS: frozenset({ToolCategory.TESTS, ToolCategory.PERFORMANCE}),  # k6: S9
    GateName.STATIC: frozenset({ToolCategory.LINT, ToolCategory.TYPES}),
    GateName.SECURITY: frozenset(
        {ToolCategory.SECRETS, ToolCategory.SAST, ToolCategory.DEPENDENCIES}
    ),
    GateName.COVERAGE: frozenset(),
    GateName.REVIEW: frozenset(),
}


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """Result of one gate with its explanation and evidence (AC-06)."""

    gate: GateName
    result: GateResult
    blocking: bool
    reason: str
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "gate": self.gate.value,
            "result": self.result.value,
            "blocking": self.blocking,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class GateReport:
    """All gate outcomes and the overall verdict."""

    outcomes: tuple[GateOutcome, ...]
    verdict: GateResult
    new_findings: tuple[Finding, ...]
    suppressed: tuple[Finding, ...]
    expired_baseline: tuple[Finding, ...]
    strict: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "verdict": self.verdict.value,
            "strict": self.strict,
            "gates": [o.to_dict() for o in self.outcomes],
            "baseline": {
                "suppressed": len(self.suppressed),
                "expired": len(self.expired_baseline),
            },
            "new_findings": [f.fingerprint for f in self.new_findings],
            "notes": list(self.notes),
        }


def _evidence(findings: Iterable[Finding]) -> tuple[str, ...]:
    items = [
        f"{f.tool}:{f.rule_id} {f.path or '-'}:{f.line or '-'} ({f.severity.value})"
        for f in findings
    ]
    if len(items) > _MAX_EVIDENCE:
        items = [*items[:_MAX_EVIDENCE], f"… and {len(items) - _MAX_EVIDENCE} more"]
    return tuple(items)


class QualityGateEngine:
    """Evaluates the configured gates."""

    def __init__(self, config: QualityGatesConfig, *, strict: bool = False) -> None:
        self._cfg = config
        self._strict = strict

    def evaluate(
        self,
        runs: Sequence[ToolRun],
        baseline: Baseline,
        *,
        today: date | None = None,
    ) -> GateReport:
        """Compute every gate from ``runs`` (and the baseline)."""
        all_findings = [f for r in runs for f in r.findings]
        new, suppressed, expired = baseline.partition(all_findings, today=today)
        # Expired exceptions no longer suppress: those findings count like new ones.
        new_fp = {f.fingerprint for f in new} | {f.fingerprint for f in expired}
        outcomes = [
            self._security(runs, new_fp),
            self._static(runs, new_fp),
            self._tool_gate(
                GateName.BUILD, self._cfg.build.enabled, self._cfg.build.blocking, runs
            ),
            self._tool_gate(
                GateName.TESTS, self._cfg.tests.enabled, self._cfg.tests.blocking, runs
            ),
            self._coverage(runs),
            self._review(),
        ]
        failed = any(o.result is GateResult.FAIL and o.blocking for o in outcomes)
        strict_failed = self._strict and any(
            o.result is GateResult.SKIPPED and o.blocking and "disabled" not in o.reason
            for o in outcomes
        )
        notes = []
        if strict_failed:
            notes.append("strict mode: a blocking gate was SKIPPED")
        if expired:
            notes.append(
                f"{len(expired)} finding(s) match expired baseline entries and count again"
            )
        verdict = GateResult.FAIL if failed or strict_failed else GateResult.PASS
        order = list(GateName)
        return GateReport(
            outcomes=tuple(sorted(outcomes, key=lambda o: order.index(o.gate))),
            verdict=verdict,
            new_findings=tuple(new),
            suppressed=tuple(suppressed),
            expired_baseline=tuple(expired),
            strict=self._strict,
            notes=tuple(notes),
        )

    @staticmethod
    def _relevant(runs: Sequence[ToolRun], gate: GateName) -> list[ToolRun]:
        """Runs that produced (or failed to produce) a result; skipped tools don't count."""
        return [r for r in runs if r.category in _GATE_CATEGORIES[gate] and not r.skipped]

    @staticmethod
    def _skip_reason(runs: Sequence[ToolRun], gate: GateName, default: str) -> str:
        skipped = [r for r in runs if r.category in _GATE_CATEGORIES[gate] and r.skipped]
        if not skipped:
            return default
        return "tools not applicable: " + "; ".join(f"{r.tool}: {r.error}" for r in skipped)

    def _broken(self, gate: GateName, blocking: bool, runs: list[ToolRun]) -> GateOutcome | None:
        failed = [r for r in runs if not r.succeeded]
        if not failed:
            return None
        return GateOutcome(
            gate,
            GateResult.FAIL,
            blocking,
            "tool(s) did not produce a result; failing closed: "
            + "; ".join(f"{r.tool}: {r.error}" for r in failed),
            tuple(f"{r.tool}: {r.error}" for r in failed),
        )

    def _security(self, runs: Sequence[ToolRun], new_fp: set[str]) -> GateOutcome:
        cfg = self._cfg.security
        gate = GateName.SECURITY
        if not cfg.enabled:
            return GateOutcome(
                gate, GateResult.SKIPPED, cfg.blocking, "gate disabled in configuration"
            )
        relevant = self._relevant(runs, gate)
        if not relevant:
            return GateOutcome(gate, GateResult.SKIPPED, cfg.blocking,
                               self._skip_reason(runs, gate, "no security tool ran"))  # fmt: skip
        broken = self._broken(gate, cfg.blocking, relevant)
        if broken:
            return broken
        offending = [
            f
            for r in relevant
            for f in r.findings
            if f.fingerprint in new_fp
            and (
                (r.category is ToolCategory.SECRETS and cfg.block_new_secrets)
                or f.severity.rank <= cfg.fail_on.rank
            )
        ]
        tools = ", ".join(f"{r.tool} {r.version or '?'}" for r in relevant)
        if offending:
            return GateOutcome(
                gate,
                GateResult.FAIL,
                cfg.blocking,
                f"{len(offending)} finding(s) at or above '{cfg.fail_on.value}' "
                f"or new secrets ({tools})",
                _evidence(sorted(offending, key=lambda f: f.severity.rank)),
            )
        return GateOutcome(gate, GateResult.PASS, cfg.blocking, f"no blocking findings ({tools})")

    def _static(self, runs: Sequence[ToolRun], new_fp: set[str]) -> GateOutcome:
        cfg = self._cfg.static
        gate = GateName.STATIC
        if not cfg.enabled:
            return GateOutcome(
                gate, GateResult.SKIPPED, cfg.blocking, "gate disabled in configuration"
            )
        relevant = self._relevant(runs, gate)
        if not relevant:
            return GateOutcome(gate, GateResult.SKIPPED, cfg.blocking, self._skip_reason(
                runs, gate, "no linter/type checker applies to the project stacks"))  # fmt: skip
        broken = self._broken(gate, cfg.blocking, relevant)
        if broken:
            return broken
        errors = [f for r in relevant for f in r.findings if f.fingerprint in new_fp]
        tools = ", ".join(f"{r.tool} {r.version or '?'}" for r in relevant)
        if len(errors) > cfg.max_errors:
            return GateOutcome(
                gate,
                GateResult.FAIL,
                cfg.blocking,
                f"{len(errors)} lint/type issue(s) > threshold {cfg.max_errors} ({tools})",
                _evidence(errors),
            )
        reason = f"{len(errors)} issue(s) ≤ threshold {cfg.max_errors} ({tools})"
        return GateOutcome(gate, GateResult.PASS, cfg.blocking, reason)

    def _tool_gate(
        self, gate: GateName, enabled: bool, blocking: bool, runs: Sequence[ToolRun]
    ) -> GateOutcome:
        if not enabled:
            return GateOutcome(gate, GateResult.SKIPPED, blocking, "gate disabled in configuration")
        relevant = self._relevant(runs, gate)
        if not relevant:
            return GateOutcome(gate, GateResult.SKIPPED, blocking, self._skip_reason(
                runs, gate, f"no {gate.value} tool applies to the project stacks"))  # fmt: skip
        broken = self._broken(gate, blocking, relevant)
        if broken:
            return broken
        failures = [f for r in relevant for f in r.findings]
        if failures:
            return GateOutcome(gate, GateResult.FAIL, blocking,
                               f"{len(failures)} failure(s)", _evidence(failures))  # fmt: skip
        return GateOutcome(gate, GateResult.PASS, blocking, "all required checks passed")

    def _coverage(self, runs: Sequence[ToolRun]) -> GateOutcome:
        cfg = self._cfg.coverage
        gate = GateName.COVERAGE
        if not cfg.enabled:
            return GateOutcome(gate, GateResult.SKIPPED, cfg.blocking,
                               "gate disabled in configuration")  # fmt: skip
        measured = [r for r in runs if r.category is ToolCategory.TESTS and not r.skipped
                    and r.succeeded and r.coverage is not None]  # fmt: skip
        if not measured:
            return GateOutcome(gate, GateResult.SKIPPED, cfg.blocking,
                               "no test tool reported coverage")  # fmt: skip
        lowest = min(measured, key=lambda r: r.coverage or 0.0)
        value = float(lowest.coverage or 0.0)
        details = ", ".join(f"{r.tool} {r.coverage:.1f}%" for r in measured)
        if value < cfg.min_percent:
            return GateOutcome(gate, GateResult.FAIL, cfg.blocking,
                               f"coverage {value:.1f}% < {cfg.min_percent:.1f}% ({details})",
                               (f"{lowest.tool}: {value:.1f}%",))  # fmt: skip
        return GateOutcome(gate, GateResult.PASS, cfg.blocking,
                           f"coverage ≥ {cfg.min_percent:.1f}% ({details})")  # fmt: skip

    def _review(self) -> GateOutcome:
        cfg = self._cfg.review
        if not cfg.enabled:
            return GateOutcome(GateName.REVIEW, GateResult.SKIPPED, cfg.blocking,
                               "gate disabled in configuration")  # fmt: skip
        # Human approvals of a change are evaluated on pull requests (GitHub adapter, S4).
        return GateOutcome(GateName.REVIEW, GateResult.SKIPPED, cfg.blocking,
                           "no change under review (read-only audit)")  # fmt: skip
