"""Per-test results of runs from their JUnit reports (ADR-0044).

The tests of a run leave JUnit reports as artifacts named ``junit-*.xml``: the sandboxed
tests of DEVELOP, MODERNIZE and MAINTAIN one per step (``junit-<task>.xml``; the step
that runs them several times keeps its latest), the test tools of an audit one per tool
(``junit-<tool>.xml``). This service reads them back, parsed safely, and says why there
are none (the tests were skipped — with the setting behind it — or the command writes no
report). An audit's results are all its tools' reports; another run's are its latest
report (the tests of the final change), the others are listed. Each run is compared with
the project's previous run that has results: tests that passed there and fail now are its
regressions. Parsed reports are cached by content digest: an artifact never changes.
"""

from __future__ import annotations

import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_coding.api.core import InvalidError, Principal, detached_project, iso_or_none
from ai_coding.api.project_settings import ProjectSettingsService
from ai_coding.api.rbac import Permission
from ai_coding.orchestrator.engine import make_artifact_store
from ai_coding.persistence.artifacts import ArtifactIntegrityError, ArtifactStore
from ai_coding.persistence.models import Artifact, Project, Run, Task
from ai_coding.quality.junit import JUnitError, TestCase, TestSuite, parse_junit, summarize

REPORT = re.compile(r"^junit-(?P<tool>[\w.-]+)\.xml$")
RECENT_RUNS = 15
TEST_KINDS = ("develop", "maintain", "modernize", "audit")
FAILING = ("failed", "error")
_CACHE_SIZE = 256
_LISTED = 20  # failing tests / regressions listed per run in the project view
_BRIEF = ("key", "name", "classname", "file", "line", "status", "message")
_ORDER = {"failed": 0, "error": 1, "passed": 2, "skipped": 3}
_SETTINGS = (
    ("tests gate disabled", "quality_gates.tests.enabled"),
    ("no test setup detected", "quality_gates.tests.command"),
    ("execution.docker.network", "execution.docker.network"),
    ("security policy forbids", "quality_gates.tests.install_hosts"),
    ("Docker sandbox unavailable", "execution.backend"),
    ("sandbox image", "execution.docker"),
)


def _setting_for(reason: str) -> str | None:
    """The setting behind a test skip reason (see ``implement.run_tests``)."""
    return next((setting for needle, setting in _SETTINGS if needle in reason), None)


def _brief(case: TestCase) -> dict[str, Any]:
    data = case.to_dict()
    return {k: data[k] for k in _BRIEF}


@dataclass
class _Results:
    """The parsed reports of one run."""

    suites: list[TestSuite]
    reports: list[dict[str, Any]]
    problems: list[str]

    def statuses(self) -> dict[str, TestCase]:
        return {c.key: c for st in self.suites for c in st.cases}


class TestResultService(ProjectSettingsService):
    """Per-test results of runs from their JUnit reports (ADR-0044)."""

    __test__ = False  # not a pytest test class
    _parsed: ClassVar[OrderedDict[str, list[TestSuite] | str]] = OrderedDict()
    _parsed_lock: ClassVar[Lock] = Lock()

    # -------------------------------------------------------------- reports --
    def _parse(self, art: Artifact, store: ArtifactStore) -> list[TestSuite] | str:
        """The suites of a report, or why it cannot be read."""
        with self._parsed_lock:
            cached = self._parsed.get(art.sha256)
        if cached is not None:
            return cached
        match = REPORT.match(art.name)
        tool = match["tool"] if match else "junit"
        try:
            parsed: list[TestSuite] | str = parse_junit(store.get(art.sha256), tool=tool)
        except JUnitError as exc:
            parsed = str(exc)
        except FileNotFoundError:
            parsed = "the report is no longer stored (artifact retention)"
        except (OSError, RuntimeError, ArtifactIntegrityError) as exc:
            return f"the report could not be read: {str(exc)[:200]}"  # not cached: transient
        with self._parsed_lock:
            self._parsed[art.sha256] = parsed
            while len(self._parsed) > _CACHE_SIZE:
                self._parsed.popitem(last=False)
        return parsed

    def _results(self, s: Session, project: Project, run: Run) -> _Results:
        rows = s.scalars(select(Artifact).where(Artifact.run_id == run.id))
        latest: dict[str, Artifact] = {}
        for art in sorted(rows, key=lambda a: (a.created_at, str(a.id))):
            if REPORT.match(art.name):
                latest.pop(art.name, None)  # re-insert: dict order = order of the last report
                latest[art.name] = art
        if not latest:
            return _Results([], [], [])
        root, config = self.project_config(project)
        store = make_artifact_store(config, root)
        primary = set(latest) if run.options.get("kind") == "audit" else {list(latest)[-1]}
        suites: list[TestSuite] = []
        reports: list[dict[str, Any]] = []
        problems: list[str] = []
        for name, art in latest.items():
            parsed = self._parse(art, store)
            report: dict[str, Any] = {"name": name, "created_at": iso_or_none(art.created_at),
                                      "primary": name in primary}  # fmt: skip
            if isinstance(parsed, str):
                problems.append(f"{name}: {parsed}")
            else:
                report["totals"] = summarize(parsed)
                if name in primary:
                    suites.extend(parsed)
            reports.append(report)
        suites.sort(key=lambda st: (min((_ORDER.get(c.status, 9) for c in st.cases), default=9),
                                    st.name))  # fmt: skip
        return _Results(suites, reports, problems)

    @staticmethod
    def _why_none(kind: str | None, outputs: list[tuple[str, Any]]) -> tuple[str, str | None]:
        """Why a run has no per-test results, and the setting that decides it."""
        for key, out in outputs:
            tests = out.get("tests") if isinstance(out, dict) else None
            if isinstance(tests, dict) and tests.get("status"):
                reason = str(tests.get("reason") or "")
                if tests["status"] == "skipped":
                    return f"tests skipped: {reason or 'no reason given'}", _setting_for(reason)
                return (
                    f"tests {tests['status']} ({key}) without a JUnit report: the test command "
                    "writes none to /deps/junit.xml",
                    "quality_gates.tests.command",
                )
        tools = [o for _, o in outputs if isinstance(o, dict) and o.get("category") == "tests"]
        failed = [o for o in tools if o.get("skipped") or not o.get("succeeded")]
        if failed:
            why = "; ".join(f"{o.get('tool')}: {o.get('error') or 'did not run'}" for o in failed)
            return f"test tools did not run: {why}", None
        if kind == "audit":
            reason = (
                "the audit's test tools give no per-test output"
                if tools
                else "this audit ran no test tool for the project's stacks (Python and "
                "Node.js tests run in DEVELOP, MODERNIZE and MAINTAIN)"
            )
        elif kind in TEST_KINDS:
            reason = "the run has not reached its tests"
        else:
            reason = f"a {kind or 'such'} run does not run tests"
        return reason, None

    def _payload(
        self, s: Session, run: Run, results: _Results, before: _Results | None, before_id: str
    ) -> dict[str, Any]:
        reason = setting = None
        if not results.suites:
            rows = s.execute(select(Task.key, Task.output).where(Task.run_id == run.id))
            reason, setting = self._why_none(run.options.get("kind"), [(k, o) for k, o in rows])
        regressions: list[dict[str, Any]] = []
        fixed: list[str] = []
        if before is not None and results.suites:
            was = before.statuses()
            for key, case in results.statuses().items():
                old = was.get(key)
                if old is None:
                    continue
                if case.status in FAILING and old.status == "passed":
                    regressions.append(_brief(case))
                elif case.status == "passed" and old.status in FAILING:
                    fixed.append(key)
        return {
            "run_id": str(run.id),
            "kind": run.options.get("kind"),
            "state": run.state,
            "goal": run.goal,
            "created_at": iso_or_none(run.created_at),
            "totals": summarize(results.suites),
            "suites": [st.to_dict() for st in results.suites],
            "reports": results.reports,
            "problems": results.problems,
            "reason": reason,
            "setting": setting,
            "compared_with": before_id if before is not None else None,
            "regressions": regressions,
            "fixed": fixed,
        }

    def _previous(self, s: Session, project: Project, run: Run) -> tuple[Run, _Results] | None:
        """The project's latest earlier run that has per-test results."""
        query = (
            select(Run)
            .where(Run.project_id == run.project_id, Run.created_at < run.created_at)
            .order_by(Run.created_at.desc())
            .limit(RECENT_RUNS)
        )
        for older in s.scalars(query):
            if older.options.get("kind") in TEST_KINDS:
                results = self._results(s, project, older)
                if results.suites:
                    return older, results
        return None

    # ------------------------------------------------------------------ API --
    def run_tests(self, principal: Principal, run_id: uuid.UUID) -> dict[str, Any]:
        """The per-test results of one run, compared with the previous run that has any."""
        with self.session() as s:
            run, _ = self._run(s, principal, run_id, Permission.RUN_VIEW)
            project = detached_project(s, run.project_id)
            results = self._results(s, project, run)
            previous = self._previous(s, project, run) if results.suites else None
            before, before_id = (previous[1], str(previous[0].id)) if previous else (None, "")
            return self._payload(s, run, results, before, before_id)

    def project_test_runs(
        self, principal: Principal, project_id: uuid.UUID, limit: int = RECENT_RUNS
    ) -> list[dict[str, Any]]:
        """The project's recent runs that run tests: totals, failing tests, regressions."""
        limit = max(1, min(limit, 50))
        with self.session() as s:
            self.require(s, principal, project_id, Permission.RUN_VIEW)
            project = detached_project(s, project_id)
            query = select(Run).where(Run.project_id == project_id)
            runs = [
                r
                for r in s.scalars(query.order_by(Run.created_at.desc()).limit(limit * 3))
                if r.options.get("kind") in TEST_KINDS
            ][:limit]
            parsed = [(r, self._results(s, project, r)) for r in runs]
            out = []
            for i, (run, results) in enumerate(parsed):
                older = next(((r, x) for r, x in parsed[i + 1 :] if x.suites), None)
                if older is None and results.suites:
                    older = self._previous(s, project, runs[-1])
                before, before_id = (older[1], str(older[0].id)) if older else (None, "")
                data = self._payload(s, run, results, before, before_id)
                cases = [c for st in data.pop("suites") for c in st["cases"]]
                failing = [c for c in cases if c["status"] in FAILING]
                data["failing"] = [{k: c[k] for k in _BRIEF} for c in failing[:_LISTED]]
                data["regressions"] = data["regressions"][:_LISTED]
                data["fixed"] = data["fixed"][:_LISTED]
                out.append(data)
            return out

    def test_history(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        key: str | None,
        limit: int = RECENT_RUNS,
    ) -> list[dict[str, Any]]:
        """One test (``classname::name``) across the project's recent runs with results."""
        limit = max(1, min(limit, 50))
        with self.session() as s:
            self.require(s, principal, project_id, Permission.RUN_VIEW)  # access first
            if not key or not key.strip():
                raise InvalidError("the test query parameter (classname::name) is required")
            project = detached_project(s, project_id)
            query = select(Run).where(Run.project_id == project_id)
            history = []
            for run in s.scalars(query.order_by(Run.created_at.desc()).limit(limit * 3)):
                if run.options.get("kind") not in TEST_KINDS:
                    continue
                results = self._results(s, project, run)
                if not results.suites:
                    continue
                case = results.statuses().get(key)
                history.append({
                    "run_id": str(run.id), "kind": run.options.get("kind"),
                    "created_at": iso_or_none(run.created_at),
                    "status": case.status if case else "absent",
                    "time": round(case.time, 4) if case else None,
                    "message": case.message[:1000] if case else "",
                })  # fmt: skip
                if len(history) >= limit:
                    break
            return history


__all__ = ["TestResultService"]
